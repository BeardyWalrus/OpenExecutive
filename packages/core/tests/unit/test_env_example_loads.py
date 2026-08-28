"""`.env.example` must survive being copied to `.env` verbatim.

That copy is the documented first step of setup, so anything in the example
file that `Settings` cannot parse breaks a fresh checkout before it starts.
It did: two keys shipped as `KEY=   # explanation`, and python-dotenv only
strips an inline comment when a value precedes it — with the value blank, the
comment text became the value. `DISCORD_NOTIFY_CHANNEL_ID` then failed int
parsing and the API died during lifespan startup.

The quieter half was worse. `TELEGRAM_BOT_TOKEN` and `DISCORD_BOT_TOKEN` are
`str | None`, so they silently took the comment text as their value — a
*truthy* string. Guards like ``if not settings.telegram_bot_token`` therefore
passed, and the outbound paths called the APIs with a comment as the token
instead of cleanly skipping an unconfigured integration.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from openexecutive.config import Settings

_REPO_ROOT = Path(__file__).resolve().parents[4]
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"

# KEY=<only whitespace># … — a blank value followed by an inline comment.
# A comment after a real value is fine; dotenv strips that correctly.
_BLANK_THEN_COMMENT = re.compile(r"^([A-Z_0-9]+)=[ \t]*#")


def _example_lines() -> list[str]:
    if not _ENV_EXAMPLE.exists():  # pragma: no cover - depends on checkout layout
        pytest.skip(f"{_ENV_EXAMPLE} not found")
    return _ENV_EXAMPLE.read_text().splitlines()


def test_no_inline_comment_on_a_blank_value() -> None:
    """Comments belong on their own line above the key, not after a blank `=`."""
    offenders = [
        f"{lineno}: {line}"
        for lineno, line in enumerate(_example_lines(), start=1)
        if _BLANK_THEN_COMMENT.match(line)
    ]
    assert not offenders, (
        "These lines put a comment after an empty value, so dotenv hands the "
        "comment text through as the value. Move the comment to its own line "
        "above the key:\n  " + "\n  ".join(offenders)
    )


def test_settings_loads_from_a_verbatim_copy(tmp_path: Path) -> None:
    """The documented `cp .env.example .env` must produce a usable config."""
    env_file = tmp_path / ".env"
    env_file.write_text(_ENV_EXAMPLE.read_text())

    settings = Settings(_env_file=str(env_file))  # type: ignore[call-arg]

    # Blank optional numerics read as unset rather than exploding on "".
    assert settings.discord_notify_channel_id is None
    assert settings.discord_guild_ids == []
    # A commented value further down the file is still parsed normally, which
    # confirms the fix did not simply strip every comment out of the example.
    assert settings.outbound_max_per_recipient_per_window == 5
    assert settings.discord_thread_response_gate_enabled is True


@pytest.mark.parametrize(
    "field",
    [
        "telegram_bot_token",
        "telegram_webhook_secret",
        "discord_bot_token",
        "discord_app_id",
        "google_chat_project_number",
    ],
)
def test_unset_credentials_are_falsy(field: str, tmp_path: Path) -> None:
    """An unconfigured integration must not look configured.

    Every call site gates on truthiness (e.g. ``if not
    settings.discord_bot_token: return``). A credential that is merely
    *present but wrong* — comment text, as before the fix — sails past those
    guards and reaches the network.
    """
    env_file = tmp_path / ".env"
    env_file.write_text(_ENV_EXAMPLE.read_text())

    settings = Settings(_env_file=str(env_file))  # type: ignore[call-arg]

    assert not getattr(settings, field), (
        f"{field} is truthy from an unedited .env.example, so an "
        f"unconfigured integration would be treated as configured"
    )
