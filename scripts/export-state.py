#!/usr/bin/env python3
"""Package a native install's accumulated state for the Docker volume.

Moving a running install into containers means moving three things, and they
do not live under one tidy directory:

    company/            profile, uploaded docs, MCP config, client slots
    chroma_db/          the vector store the knowledge search reads
    episodic_memory.db  people, decisions, initiatives, sessions, audit

The first two resolve against the repo root (`config.py` walks up for the
directory holding `.env`), but the database path is **cwd-relative** —
`DB_PATH = Path(os.environ.get("EPISODIC_DB_PATH", "./episodic_memory.db"))`
— and `make dev` runs uvicorn after `cd packages/core`, so it lands in
`packages/core/`, not beside the other two. A copy that assumes one data
directory silently leaves the database behind, and the container comes up
with an empty company.

This resolves each path the way the app itself does, honours the env
overrides when an install has moved them, and writes a tarball laid out the
way the API container expects to find `/data`.

    python3 scripts/export-state.py
    python3 scripts/export-state.py -o /tmp/openexec-state.tar.gz

SQLite files are copied through the backup API rather than read off disk, so
a WAL that has not been checkpointed still exports as one consistent file.
The vector store's binary index segments have no such guarantee, which is why
this refuses to run while the API is up unless you pass --force.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path

# Where the container expects each piece under /data. Keep in step with
# docker/docker-compose.ghcr.yml (VECTOR_STORE_PATH, COMPANY_PROFILE_PATH,
# EPISODIC_DB_PATH) and docker/Dockerfile.
VOLUME_LAYOUT = {
    "company": "company",
    "chroma": "chroma_db",
    "db": "episodic_memory.db",
}


def repo_root() -> Path:
    """The directory `config.py` resolves paths against: the one holding `.env`.

    Mirrors the walk in config.py, including its fallback, so this script and
    the app agree about where the state is even on an unusual checkout.
    """
    here = Path(__file__).resolve().parent
    root = here
    while root.parent != root:
        if (root / ".env").exists():
            return root
        root = root.parent
    return Path.cwd()


def read_env_file(env_file: Path) -> dict[str, str]:
    """The `KEY=value` pairs from `.env`, which is where overrides really live.

    `Settings` loads these through pydantic-settings, and `.env.example`
    documents VECTOR_STORE_PATH / COMPANY_PROFILE_PATH / EPISODIC_DB_PATH as
    `.env` entries — so reading only `os.environ` would miss the configuration
    of every install that moved its data the documented way, and quietly export
    the wrong files. Deliberately minimal: no interpolation, no `export`
    handling, just enough to recover a path.
    """
    values: dict[str, str] = {}
    try:
        lines = env_file.read_text().splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #")[0].strip().strip("\"'")
        if value:
            values[key.strip()] = value
    return values


class MissingOverride(Exception):
    """An explicitly configured path does not exist on disk."""


def _override(name: str, env_file_values: dict[str, str]) -> str | None:
    """A real environment variable wins over `.env`, as pydantic-settings does."""
    return os.environ.get(name) or env_file_values.get(name)


def resolve_sources(root: Path) -> dict[str, Path | None]:
    """Locate each piece of state, preferring an explicit override.

    Raises MissingOverride when a path was configured explicitly but is not
    there: that means the config and the disk disagree, and guessing at another
    location risks exporting stale data under a confident "add" line.
    """
    configured = read_env_file(root / ".env")

    profile = _override("COMPANY_PROFILE_PATH", configured)
    if profile:
        company = Path(profile).expanduser()
        if not company.is_absolute():
            company = root / company
        company = company.resolve().parent
        if not company.exists():
            raise MissingOverride(f"COMPANY_PROFILE_PATH points at {company}, which does not exist")
    else:
        company = root / "company"

    chroma_cfg = _override("VECTOR_STORE_PATH", configured)
    if chroma_cfg:
        chroma = Path(chroma_cfg).expanduser()
        if not chroma.is_absolute():
            chroma = root / chroma
        chroma = chroma.resolve()
        if not chroma.exists():
            raise MissingOverride(f"VECTOR_STORE_PATH points at {chroma}, which does not exist")
    else:
        chroma = root / "chroma_db"

    db_cfg = _override("EPISODIC_DB_PATH", configured)
    if db_cfg:
        db: Path | None = Path(db_cfg).expanduser()
        if not db.is_absolute():
            db = root / db
        db = db.resolve()
        if not db.exists():
            raise MissingOverride(f"EPISODIC_DB_PATH points at {db}, which does not exist")
    else:
        # Unset means cwd-relative, and the cwd depends on how it was started.
        # `make dev` cds into packages/core; running uvicorn from the repo root
        # leaves it there instead. Check both rather than guess.
        candidates = [root / "packages" / "core" / "episodic_memory.db", root / "episodic_memory.db"]
        db = next((c for c in candidates if c.exists()), None)

    return {"company": company, "chroma": chroma, "db": db}


# The API's ASGI target, as it appears in the server's argv.
_APP_TARGET = "openexecutive.api.main:app"


def api_is_running() -> str | None:
    """The command line of a running API server, if there is one.

    Matching the app target against whole command lines is not enough: a shell
    invoked with that string anywhere in its arguments — a `make` recipe, a
    `pkill` pattern, an editor, this script's own launcher — matches too, and
    a false positive here blocks an export for no reason. So require the
    process to actually *be* a python or uvicorn interpreter, by checking
    argv[0], and read /proc directly so no pgrep is needed.
    """
    me = os.getpid()
    try:
        pids = [pid for pid in os.listdir("/proc") if pid.isdigit()]
    except OSError:
        return None  # Not a Linux /proc — trust the operator.
    for pid in pids:
        if int(pid) == me:
            continue
        try:
            # Binary: any process on the machine may carry non-UTF-8 bytes in
            # its argv (a latin-1 filename, a binary argument), and decoding
            # that strictly would abort an export over a stranger's process.
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                raw = fh.read().decode("utf-8", "replace")
        except OSError:
            continue  # Exited between the listing and the read.
        argv = [a for a in raw.split(chr(0)) if a]
        if not argv or not any(_APP_TARGET in a for a in argv[1:]):
            continue
        exe = Path(argv[0]).name
        if "python" in exe or exe == "uvicorn":
            # Report the pid and the interpreter only. A full command line can
            # carry credentials passed as arguments, and this is printed to a
            # terminal and possibly pasted into an issue.
            return f"pid {pid} ({exe}, serving {_APP_TARGET})"
    return None


def copy_sqlite(src: Path, dst: Path) -> None:
    """Copy a SQLite database consistently, WAL and all.

    `shutil.copy` of a live database can capture a torn page or miss a WAL
    that has not been checkpointed; the backup API takes a read lock and
    produces one self-contained file.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    # Build the URI with as_uri(), which percent-escapes. Interpolating the raw
    # path lets a filename containing `?` truncate it or inject further URI
    # parameters ahead of mode=ro, making the read-only guarantee depend on
    # what a file happens to be called.
    source = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def copy_tree(src: Path, dst: Path) -> list[str]:
    """Copy a directory, routing any SQLite file through the backup API.

    Returns warnings worth showing the operator. Symlinks are skipped rather
    than followed: `shutil.copy2` would archive the *target's contents* as a
    regular file, so a link planted in `company/docs` — and this repo teaches
    the symlink idiom, `make link-env` creates one — would sweep whatever it
    points at into an archive that then travels to another host. The README
    promises the `.env` does not come across; following links would break that
    promise silently.
    """
    warnings: list[str] = []
    for item in sorted(src.rglob("*")):
        rel = item.relative_to(src)
        out = dst / rel
        if item.is_symlink():
            warnings.append(f"skipped symlink {rel} -> {os.readlink(item)}")
            continue
        if item.is_dir():
            out.mkdir(parents=True, exist_ok=True)
        elif item.name.endswith(("-wal", "-shm")):
            # Folded into the backup of their parent database; copying them
            # separately would put a stale journal beside a consistent file.
            continue
        elif item.suffix in (".sqlite3", ".db", ".sqlite"):
            try:
                copy_sqlite(item, out)
            except sqlite3.DatabaseError:
                # Named like a database but isn't one. Copy it verbatim rather
                # than aborting an otherwise good export over one stray file.
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, out)
                warnings.append(f"{rel} is not a SQLite database — copied as a plain file")
        else:
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, out)
    return warnings


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}B" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "-o",
        "--output",
        default="openexec-state.tar.gz",
        help="tarball to write (default: openexec-state.tar.gz)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="export even though the API is running (vector index may tear)",
    )
    args = parser.parse_args()

    running = api_is_running()
    if running and not args.force:
        print(f"The API is still running:\n  {running}\n", file=sys.stderr)
        print(
            "Its vector-store index files can be copied mid-write. Stop it first\n"
            "(`make stop`, matching API_PORT/UI_PORT if you overrode them), or pass\n"
            "--force if you know it is idle.",
            file=sys.stderr,
        )
        return 1

    root = repo_root()
    try:
        sources = resolve_sources(root)
    except MissingOverride as exc:
        print(f"{exc}\n", file=sys.stderr)
        print(
            "Fix the path (in `.env` or the environment) before exporting. Guessing\n"
            "at another location risks shipping stale data under a confident\n"
            '"add" line — the worst possible outcome for a migration.',
            file=sys.stderr,
        )
        return 1
    print(f"Repo root: {root}")

    staged: list[str] = []
    warnings: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "data"
        stage.mkdir()

        for key, src in sources.items():
            dest_name = VOLUME_LAYOUT[key]
            if src is None or not src.exists():
                print(f"  skip  {dest_name:<20} (not found — nothing to migrate)")
                continue
            if src.is_dir():
                warnings.extend(copy_tree(src, stage / dest_name))
            else:
                copy_sqlite(src, stage / dest_name)
            print(f"  add   {dest_name:<20} <- {src}")
            staged.append(dest_name)

        if not staged:
            print("\nNothing to export — no state found.", file=sys.stderr)
            return 1

        out = Path(args.output).expanduser().resolve()
        with tarfile.open(out, "w:gz") as tar:
            for name in sorted(staged):
                tar.add(stage / name, arcname=name)
        # The staging directory is 0700; the durable artifact holding company
        # documents, client slots and the audit database must not be left
        # world-readable for every local account and backup agent to pick up.
        out.chmod(0o600)

    if warnings:
        print("\nWarnings:")
        for warning in warnings:
            print(f"  {warning}")

    print(f"\nWrote {out} ({human(out.stat().st_size)}, mode 0600)")
    print(
        "\nOn the Docker host, with this file and the compose file present:\n"
        f"  docker compose --env-file .env -f docker/docker-compose.ghcr.yml run --rm \\\n"
        f"    --no-deps -v \"$(pwd)/{out.name}:/state.tar.gz:ro\" \\\n"
        "    api tar --no-same-owner --no-same-permissions \\\n"
        "      -xzf /state.tar.gz -C /data\n"
        "  docker compose --env-file .env -f docker/docker-compose.ghcr.yml up -d"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
