from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("clawchat_gateway.greeting")

# Worded to make the greeting the single required action. Earlier wording
# ("ClawChat activation bootstrap: ... do both ...") led some agents to treat it
# as a setup task and write a file instead of replying, so no greeting reached
# the user. Lead with the chat reply, forbid file/tool detours, and keep the
# profile update strictly optional and secondary. (Mirrors the openclaw plugin.)
ACTIVATION_BOOTSTRAP_PROMPT = (
    "You are now connected to a ClawChat direct conversation with your user.\n\n"
    "Reply now with one short, friendly greeting message in this conversation: "
    "introduce yourself and say you are connected and ready.\n"
    "Send it as a normal chat reply. Do not write or create any files or notes, "
    "and do not call tools just to greet.\n"
    "Only if you already have your own profile details (display name, bio, or avatar) "
    "may you also call `clawchat_update_account_profile` (use `clawchat_upload_avatar_image` "
    "first for a local avatar image); otherwise skip that and just greet.\n\n"
    "Do not ask the user for profile information."
)

# First message to a newly added NON-owner friend. Distinct from the owner
# activation prompt: this reader is a stranger, so "you are connected and
# ready" makes no sense and the agent must say whose agent it is instead.
FRIEND_GREETING_PROMPT = (
    "A ClawChat user has just become your friend. You are now in a direct "
    "conversation with them; they are not your owner.\n\n"
    "Reply now with one short, friendly greeting message in this conversation: "
    "introduce yourself by name, say you are an AI agent acting on behalf of "
    "your owner, and invite them to tell you what they need.\n"
    "Send it as a normal chat reply. Do not write or create any files or notes, "
    "and do not call tools just to greet.\n"
    "Do not share your owner's private information, and do not ask the user "
    "for personal information."
)

# Cross-plugin, user-editable overrides live under ~/clawchat/.
_GREETING_FILE_RELPARTS = ("clawchat", "greeting.md")
_FRIEND_GREETING_FILE_RELPARTS = ("clawchat", "friend-greeting.md")


def _load_prompt_with_override(
    relparts: tuple[str, ...], default: str, home_dir: Path | None
) -> str:
    base = home_dir if home_dir is not None else Path.home()
    greeting_path = base.joinpath(*relparts)
    try:
        content = greeting_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning(
            "clawchat: failed to read greeting override %s: %s", greeting_path, exc
        )
        return default
    stripped = content.strip()
    return stripped or default


def load_activation_bootstrap_prompt(home_dir: Path | None = None) -> str:
    """Return the first-load greeting prompt.

    If ``~/clawchat/greeting.md`` exists and is non-empty after stripping, its
    content replaces the built-in prompt. A missing file, an empty/whitespace
    file, or any read error falls back to :data:`ACTIVATION_BOOTSTRAP_PROMPT` so
    greeting dispatch never fails on a bad override file. ``home_dir`` is
    injectable for tests and defaults to the real home directory.
    """
    return _load_prompt_with_override(
        _GREETING_FILE_RELPARTS, ACTIVATION_BOOTSTRAP_PROMPT, home_dir
    )


def load_friend_greeting_prompt(home_dir: Path | None = None) -> str:
    """Return the first-message prompt for a newly added non-owner friend.

    Same override contract as :func:`load_activation_bootstrap_prompt`, but the
    file is ``~/clawchat/friend-greeting.md`` and the fallback is
    :data:`FRIEND_GREETING_PROMPT`.
    """
    return _load_prompt_with_override(
        _FRIEND_GREETING_FILE_RELPARTS, FRIEND_GREETING_PROMPT, home_dir
    )
