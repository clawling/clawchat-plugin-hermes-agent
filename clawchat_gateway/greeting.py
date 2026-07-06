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

# Cross-plugin, user-editable override lives at ~/clawchat/greeting.md.
_GREETING_FILE_RELPARTS = ("clawchat", "greeting.md")


def load_activation_bootstrap_prompt(home_dir: Path | None = None) -> str:
    """Return the first-load greeting prompt.

    If ``~/clawchat/greeting.md`` exists and is non-empty after stripping, its
    content replaces the built-in prompt. A missing file, an empty/whitespace
    file, or any read error falls back to :data:`ACTIVATION_BOOTSTRAP_PROMPT` so
    greeting dispatch never fails on a bad override file. ``home_dir`` is
    injectable for tests and defaults to the real home directory.
    """
    base = home_dir if home_dir is not None else Path.home()
    greeting_path = base.joinpath(*_GREETING_FILE_RELPARTS)
    try:
        content = greeting_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ACTIVATION_BOOTSTRAP_PROMPT
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning(
            "clawchat: failed to read greeting override %s: %s", greeting_path, exc
        )
        return ACTIVATION_BOOTSTRAP_PROMPT
    stripped = content.strip()
    return stripped or ACTIVATION_BOOTSTRAP_PROMPT
