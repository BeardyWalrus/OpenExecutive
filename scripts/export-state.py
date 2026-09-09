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


def resolve_sources(root: Path) -> dict[str, Path | None]:
    """Locate each piece of state, preferring an explicit env override."""
    company_profile = os.environ.get("COMPANY_PROFILE_PATH")
    company = (
        Path(company_profile).expanduser().resolve().parent
        if company_profile
        else root / "company"
    )

    chroma_env = os.environ.get("VECTOR_STORE_PATH")
    chroma = Path(chroma_env).expanduser().resolve() if chroma_env else root / "chroma_db"

    db_env = os.environ.get("EPISODIC_DB_PATH")
    if db_env:
        db: Path | None = Path(db_env).expanduser().resolve()
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
            with open(f"/proc/{pid}/cmdline") as fh:
                argv = fh.read().split(chr(0))
        except OSError:
            continue  # Exited between the listing and the read.
        argv = [a for a in argv if a]
        if not argv or not any(_APP_TARGET in a for a in argv[1:]):
            continue
        exe = Path(argv[0]).name
        if "python" in exe or exe == "uvicorn":
            return " ".join(argv)[:120]
    return None


def copy_sqlite(src: Path, dst: Path) -> None:
    """Copy a SQLite database consistently, WAL and all.

    `shutil.copy` of a live database can capture a torn page or miss a WAL
    that has not been checkpointed; the backup API takes a read lock and
    produces one self-contained file.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def copy_tree(src: Path, dst: Path) -> None:
    """Copy a directory, routing any SQLite file through the backup API."""
    for item in sorted(src.rglob("*")):
        rel = item.relative_to(src)
        out = dst / rel
        if item.is_dir():
            out.mkdir(parents=True, exist_ok=True)
        elif item.suffix in (".sqlite3", ".db", ".sqlite"):
            copy_sqlite(item, out)
        elif item.name.endswith(("-wal", "-shm")):
            # Folded into the backup of their parent database; copying them
            # separately would put a stale journal beside a consistent file.
            continue
        else:
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, out)


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
    sources = resolve_sources(root)
    print(f"Repo root: {root}")

    staged: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "data"
        stage.mkdir()

        for key, src in sources.items():
            dest_name = VOLUME_LAYOUT[key]
            if src is None or not src.exists():
                print(f"  skip  {dest_name:<20} (not found — nothing to migrate)")
                continue
            if src.is_dir():
                copy_tree(src, stage / dest_name)
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

    print(f"\nWrote {out} ({human(out.stat().st_size)})")
    print(
        "\nOn the Docker host, with this file and the compose file present:\n"
        f"  docker compose --env-file .env -f docker/docker-compose.ghcr.yml run --rm \\\n"
        f"    --no-deps -v \"$(pwd)/{out.name}:/state.tar.gz:ro\" \\\n"
        "    api tar -xzf /state.tar.gz -C /data\n"
        "  docker compose --env-file .env -f docker/docker-compose.ghcr.yml up -d"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
