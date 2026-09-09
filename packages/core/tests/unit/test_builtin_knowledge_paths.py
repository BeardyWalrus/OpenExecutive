"""Every registered built-in doc must be readable back.

The review queue registers built-in knowledge by walking the tree recursively
and keying each row on the file's *immediate parent* directory
(`ReviewStore.sync_builtin_registrations`), while the read/write endpoints
resolved a flat `<domain>/<filename>`. The tree has two tiers —
`<domain>/*.md` and `failures/<domain>/*.md` — so every failure case study
registered under a domain whose flat path does not exist: 17 of the 81
built-in docs, each showing "Could not load file content" in the review UI.

These tests encode the contract the two halves share rather than the specific
`failures/` shape, so a third tier cannot reintroduce the same split.
"""
from __future__ import annotations

from pathlib import Path

from openexecutive.api.routes.knowledge import _resolve_path, list_builtin_files
from openexecutive.knowledge.loader import BUILTIN_KNOWLEDGE_PATH, DOMAIN_MAP


def _registered_pairs() -> list[tuple[str, str, Path]]:
    """(domain, filename, real path) for exactly what the review store records."""
    pairs = []
    for md_file in sorted(BUILTIN_KNOWLEDGE_PATH.rglob("*.md")):
        if "skills" in md_file.parts:
            continue
        pairs.append((md_file.parent.name, md_file.name, md_file))
    return pairs


def test_every_registered_builtin_doc_resolves() -> None:
    """The pair the review queue stores must round-trip to the file on disk."""
    unresolvable = [
        f"{domain}:{filename} (real path {real.relative_to(BUILTIN_KNOWLEDGE_PATH)})"
        for domain, filename, real in _registered_pairs()
        if not _resolve_path(domain, filename).exists()
    ]
    assert not unresolvable, (
        "These docs are registered by ReviewStore.sync_builtin_registrations but "
        "_resolve_path cannot find them, so the review UI shows 'Could not load "
        "file content':\n  " + "\n  ".join(unresolvable)
    )


def test_nested_failure_doc_resolves_to_its_real_path() -> None:
    """A concrete nested case — the one that surfaced this."""
    nested = BUILTIN_KNOWLEDGE_PATH / "failures" / "board" / "credit-suisse.md"
    if not nested.exists():  # pragma: no cover - depends on the shipped corpus
        return
    assert _resolve_path("board", "credit-suisse.md") == nested


def test_resolution_prefers_the_flat_path() -> None:
    """A top-level doc must never be shadowed by a nested file of the same name."""
    for domain in sorted(DOMAIN_MAP):
        flat_dir = BUILTIN_KNOWLEDGE_PATH / domain
        if not flat_dir.is_dir():
            continue
        for md_file in sorted(flat_dir.glob("*.md")):
            assert _resolve_path(domain, md_file.name) == md_file


def test_missing_file_still_returns_a_path_for_the_404() -> None:
    """Callers check `.exists()` themselves, so a miss must not raise here."""
    resolved = _resolve_path("board", "definitely-not-a-real-doc.md")
    assert not resolved.exists()


async def test_listing_includes_nested_docs() -> None:
    """The knowledge browser must show what the review queue registers."""
    listed = {(f.domain, f.filename) for f in (await list_builtin_files()).files}
    registered = {(domain, filename) for domain, filename, _ in _registered_pairs()}
    missing = registered - listed
    assert not missing, f"registered but absent from the listing: {sorted(missing)}"
