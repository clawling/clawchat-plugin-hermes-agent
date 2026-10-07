"""Where this agent's own editable files live: ``$HERMES_HOME/clawchat/``.

``greeting.md``, ``friend-greeting.md`` (prompt overrides the agent writes when
the owner asks) and ``onboarding.json`` (facts the agent records while
following the connection wiki) belong to ONE agent. Every Hermes profile is a
separate agent, so they are read from the profile home
(:func:`clawchat_gateway.hermes_home.hermes_home`, which follows the
multiplexing gateway's per-profile override) and never from ``os.environ``.

They used to live in ``~/clawchat/``, under the OS home that every profile on
the host shares. The DEFAULT profile still falls back there when its own file
is absent, so a file written before the move keeps working. A named profile
never does: the file there is another agent's (``onboarding.json`` carries a
per-agent capability tier and wiki report id).

Only a MISSING profile file falls back. A present-but-empty one is a reset, and
must win over a legacy file the agent may not know exists.
"""

from __future__ import annotations

from pathlib import Path

from clawchat_gateway.hermes_home import hermes_home

GREETING_FILE = "greeting.md"
FRIEND_GREETING_FILE = "friend-greeting.md"
ONBOARDING_FILE = "onboarding.json"

_DIRNAME = "clawchat"


def agent_files_dir() -> Path:
    """Absolute ``$HERMES_HOME/clawchat`` of the profile being served."""
    return (hermes_home() / _DIRNAME).expanduser().absolute()


def legacy_agent_files_dir() -> Path | None:
    """``~/clawchat`` — but only for the default profile; ``None`` otherwise."""
    from clawchat_gateway.storage import is_default_profile

    if not is_default_profile():
        return None
    return Path.home() / _DIRNAME


def agent_file_candidates(name: str) -> list[Path]:
    """The paths to try for ``name``, in order: profile home, then legacy."""
    paths = [agent_files_dir() / name]
    legacy = legacy_agent_files_dir()
    if legacy is not None:
        paths.append(legacy / name)
    return paths


def read_agent_file(name: str) -> str | None:
    """Text of the first existing candidate; ``None`` when none exists.

    Read errors other than "not found" propagate, so callers keep their own
    logging and fallback for an unreadable file.
    """
    for path in agent_file_candidates(name):
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
    return None
