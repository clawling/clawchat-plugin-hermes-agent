"""Make ``clawchat_gateway.adapter`` importable without a Hermes Agent checkout.

``adapter.py`` imports four names from the **host** package (``gateway.config``,
``gateway.platforms.base``). That package ships with Hermes Agent and is not a
dependency of this repository. ``hermes_source.py`` finds a checkout through
``HERMES_SOURCE_DIR``, then ``tmp/hermes`` or ``tmp/hermes-agent`` here or in the
main checkout when running from a linked worktree.

The policy below is *double unless asked*:

1. If ``gateway`` already imports (a real Hermes checkout is on ``sys.path``,
   or ``HERMES_REAL_HOST=1`` and a source with ``gateway/platforms/base.py`` is
   found and added below), the tests run against it and this file installs
   nothing.
2. Otherwise a **minimal double** is installed under the real module names.

Most of the suite is written against the double, so a found checkout does not
switch the whole session to the real host by itself. Tests that check real host
behaviour use ``hermes_source.require_hermes_source`` and run the host in a
subprocess instead. Read ``clawchat_host_is_double`` in a test to tell which one
you got. The pytest header reports the source found (or searched paths) and
real-host/double mode.

**What the double is honestly good for.** It reproduces exactly the four
imported names and nothing else. That is enough for logic that lives entirely in
this repository's own state — ``_handle_message_recall`` touches only
``self._store``, ``self._reply_preview_by_message_id``,
``self._reply_preview_order`` and ``self._last_inbound_message_id_by_chat``, all
of which ``ClawChatAdapter.__init__`` creates itself. It is **not** good for
anything that depends on host behaviour: base-class dispatch, the real
``MessageEvent`` shape the host constructs, or whether the host calls us at all.
Do not use it to claim host-side facts — use the recorded verification in
``docs/architecture.md``'s "Host MessageEvent verification (message-recall open
item)" section, including its source version, date and recheck conditions.
"""

from __future__ import annotations

import atexit
from contextlib import contextmanager
import os
import shutil
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import pytest

FAKE_SECRETS = {
    "access": "placeholder-access-secret-g10",
    "refresh": "placeholder-refresh-secret-g10",
    # Assembled at runtime so the shapes do not trip credential scanners.
    "bearer": " ".join(("Bearer", "placeholder-bearer-g10")),
    "userinfo_url": "https://" + ":".join(("user-g10", "placeholder-pass-g10")) + "@example.invalid",
    "query": "token=placeholder-query-g10",
}
FAKE_SECRETS_VALUES = tuple(FAKE_SECRETS.values()) + (
    "user-g10", "placeholder-pass-g10", "placeholder-bearer-g10", "placeholder-query-g10",
)


@pytest.fixture
def assert_no_secrets():
    """Assert recursively across caller results, exceptions, payloads and logs."""
    def expand(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, bytes):
            yield value.decode("utf-8", errors="replace")
        elif isinstance(value, dict):
            for key, item in value.items():
                yield from expand(key)
                yield from expand(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from expand(item)
        elif isinstance(value, BaseException):
            yield str(value)
            yield repr(value)
            yield "".join(traceback.format_exception(type(value), value, value.__traceback__))
        else:
            yield repr(value)

    def check(*outputs, caplog=None, secrets=FAKE_SECRETS_VALUES):
        exits = [(f"output {index}", output) for index, output in enumerate(outputs, 1)]
        if caplog is not None:
            for index, record in enumerate(caplog.records, 1):
                texts = [record.getMessage(), record.exc_text or ""]
                if record.exc_info:
                    texts.append("".join(traceback.format_exception(*record.exc_info)))
                exits.append((f"log record {index}", texts))
        for label, output in exits:
            for text in expand(output):
                for index, secret in enumerate(secrets, 1):
                    if secret and secret in text:
                        pytest.fail(f"{label} contains fake credential #{index} [redacted]", pytrace=False)

    return check


_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from hermes_source import candidates, find_hermes_source

_ORIGINAL_OS_HOME = Path.home().resolve()

# A real host reads its home while being imported below (``gateway.platforms.base``
# keeps home-derived constants), before any fixture can run. Point the whole
# session at a throwaway home first; ``_isolate_hermes_home`` then gives every
# test its own.
_SESSION_HOME = Path(tempfile.mkdtemp(prefix="clawchat-test-home-"))
atexit.register(shutil.rmtree, _SESSION_HOME, ignore_errors=True)
os.environ.update(
    HOME=str(_SESSION_HOME),
    USERPROFILE=str(_SESSION_HOME),
    LOCALAPPDATA=str(_SESSION_HOME / "AppData" / "Local"),
    HERMES_HOME=str(_SESSION_HOME / ".hermes"),
)
for _name in [n for n in os.environ if n.startswith("CLAWCHAT_")]:
    del os.environ[_name]

_HERMES_SOURCE = find_hermes_source("gateway/platforms/base.py")
_REAL_HOST_REQUESTED = os.environ.get("HERMES_REAL_HOST") == "1"
if _REAL_HOST_REQUESTED and _HERMES_SOURCE is not None and str(_HERMES_SOURCE) not in sys.path:
    sys.path.insert(0, str(_HERMES_SOURCE))


def _real_host_available() -> bool:
    try:
        import gateway.config  # noqa: F401
        import gateway.platforms.base  # noqa: F401
    except Exception:  # noqa: BLE001 — any import failure means "no real host"
        return False
    return True


_USING_DOUBLE = not _real_host_available()


def pytest_report_header():
    source = (
        str(_HERMES_SOURCE) if _HERMES_SOURCE is not None
        else "none; searched: " + ", ".join(str(path) for path in candidates())
    )
    mode = "real host" if not _USING_DOUBLE else (
        "double (HERMES_REAL_HOST=1 to use the source)" if _HERMES_SOURCE is not None
        and not _REAL_HOST_REQUESTED else "double"
    )
    return f"Hermes source: {source}; session uses {mode}"


@contextmanager
def _temporary_clawchat_platform():
    if _USING_DOUBLE:
        yield
        return
    from gateway.config import Platform
    from gateway.platform_registry import PlatformEntry, platform_registry

    previous = platform_registry.snapshot_registration("clawchat")
    with pytest.MonkeyPatch.context() as patch:
        # Platform(value) caches pseudo-members separately from the registry.
        for name in ("_value2member_map_", "_member_map_"):
            patch.setattr(Platform, name, dict(getattr(Platform, name)))
        platform_registry.register(PlatformEntry(
            name="clawchat", label="ClawChat", adapter_factory=lambda config: None,
            check_fn=lambda: True,
        ))
        current = platform_registry.snapshot_registration("clawchat")
        try:
            yield
        finally:
            platform_registry.restore_registration("clawchat", current, previous)


@pytest.fixture
def clawchat_test_platform():
    """Register the test platform only while a test constructs real-host adapters."""
    with _temporary_clawchat_platform():
        yield


def _install_double() -> None:
    import types

    class Platform(str, Enum):
        CLAWCHAT = "clawchat"

    class MessageType(str, Enum):
        TEXT = "text"
        IMAGE = "image"
        AUDIO = "audio"
        VIDEO = "video"
        FILE = "file"

    @dataclass
    class MessageEvent:
        platform: Any = None
        message_type: Any = None
        content: str = ""
        sender_id: str = ""
        chat_id: str = ""
        message_id: str | None = None
        raw_message: dict[str, Any] = field(default_factory=dict)
        metadata: dict[str, Any] = field(default_factory=dict)

        def __init__(self, **kwargs: Any) -> None:
            # The real host's MessageEvent has a wider and version-dependent
            # field set. Accept whatever the adapter passes rather than
            # pretending to know the exact signature — a double that guesses
            # field names would fail for reasons that say nothing about this
            # repository's code.
            for key, value in kwargs.items():
                setattr(self, key, value)

    @dataclass
    class SendResult:
        success: bool = False
        message_id: str | None = None
        error: str | None = None

    class BasePlatformAdapter:
        SUPPORTS_MESSAGE_EDITING = False
        REQUIRES_EDIT_FINALIZE = False
        MAX_MESSAGE_LENGTH = 4000

        def __init__(self, platform_config: Any, platform: Any) -> None:
            self.platform_config = platform_config
            self.platform = platform

    config_mod = types.ModuleType("gateway.config")
    config_mod.Platform = Platform

    base_mod = types.ModuleType("gateway.platforms.base")
    base_mod.BasePlatformAdapter = BasePlatformAdapter
    base_mod.MessageEvent = MessageEvent
    base_mod.MessageType = MessageType
    base_mod.SendResult = SendResult

    platforms_mod = types.ModuleType("gateway.platforms")
    platforms_mod.base = base_mod

    gateway_mod = types.ModuleType("gateway")
    gateway_mod.config = config_mod
    gateway_mod.platforms = platforms_mod

    sys.modules.setdefault("gateway", gateway_mod)
    sys.modules.setdefault("gateway.config", config_mod)
    sys.modules.setdefault("gateway.platforms", platforms_mod)
    sys.modules.setdefault("gateway.platforms.base", base_mod)


if _USING_DOUBLE:
    _install_double()


def _real_hermes_cli_available() -> bool:
    try:
        import hermes_cli.config  # noqa: F401
    except Exception:  # noqa: BLE001 — any import failure means "no real host"
        return False
    return True


_USING_HERMES_CLI_DOUBLE = not _real_hermes_cli_available()


def _install_hermes_cli_double() -> None:
    """Minimal ``hermes_cli.config`` double so ``clawchat_gateway.activate``
    is importable without a real Hermes checkout on ``sys.path``.

    ``activate.py`` imports these names unconditionally at module scope (it
    deliberately refuses to run outside Hermes — see its own comment). Tests
    that only exercise pure functions from that module (``evaluate_precheck``,
    ``PrecheckOutcome``) never call these helpers, so a no-op stand-in is
    enough to satisfy the import; it must never be mistaken for a real Hermes
    config store.
    """
    import types

    def _noop(*_args: object, **_kwargs: object) -> None:
        return None

    config_mod = types.ModuleType("hermes_cli.config")
    config_mod.get_config_path = lambda: "/tmp/clawchat-test-config.yaml"  # noqa: S108
    config_mod.get_env_path = lambda: "/tmp/clawchat-test.env"  # noqa: S108
    config_mod.read_raw_config = lambda: {}
    config_mod.remove_env_value = _noop
    config_mod.save_config = _noop
    config_mod.save_env_value = _noop

    hermes_cli_mod = types.ModuleType("hermes_cli")
    hermes_cli_mod.config = config_mod

    sys.modules.setdefault("hermes_cli", hermes_cli_mod)
    sys.modules.setdefault("hermes_cli.config", config_mod)


if _USING_HERMES_CLI_DOUBLE:
    _install_hermes_cli_double()


@pytest.fixture(scope="session")
def clawchat_host_is_double() -> bool:
    """True when the Hermes host is the double from this file, not real source."""
    return _USING_DOUBLE


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path_factory, monkeypatch):
    """Keep credentials, stores and legacy OS-home files off the real home."""
    # Separate from tmp_path: some tests need that directory to start empty.
    fake_os_home = tmp_path_factory.mktemp("hermes-home")
    home = fake_os_home / ".hermes"
    home.mkdir()
    local_appdata = fake_os_home / "AppData" / "Local"
    local_appdata.mkdir(parents=True)
    for name, path in (
        ("HOME", fake_os_home),
        ("USERPROFILE", fake_os_home),
        ("LOCALAPPDATA", local_appdata),
        ("HERMES_HOME", home),
    ):
        monkeypatch.setenv(name, str(path))
    for name in tuple(os.environ):
        if name.startswith("CLAWCHAT_"):
            monkeypatch.delenv(name)

    try:
        import hermes_constants
    except ImportError:
        hermes_constants = None
    set_override = getattr(hermes_constants, "set_hermes_home_override", None)
    reset_override = getattr(hermes_constants, "reset_hermes_home_override", None)
    # Clear ambient multiplex context, preserving tests' ability to set it.
    token = set_override("") if callable(set_override) and callable(reset_override) else None
    try:
        # Fail closed: a host whose resolver ignores the patched environment
        # would otherwise send every test to the developer's real home.
        from clawchat_gateway.hermes_home import hermes_home

        resolved = [hermes_home()]
        if callable(getattr(hermes_constants, "get_hermes_home", None)):
            resolved.append(Path(hermes_constants.get_hermes_home()))
        if any(path.resolve() != home.resolve() for path in resolved):
            pytest.fail(f"HERMES_HOME isolation failed: resolved {resolved}, expected {home}")
        yield home
    finally:
        if token is not None:
            reset_override(token)


@pytest.fixture(scope="session")
def original_os_home() -> Path:
    """The real home, captured before any per-test environment patching."""
    return _ORIGINAL_OS_HOME


@pytest.fixture(autouse=True)
def _restore_clawchat_senders(_isolate_hermes_home):
    """Each ClawChatAdapter registers itself as this process's sender; a test's
    adapter must not make the next test look like a running gateway."""
    from clawchat_gateway import terminal_send

    saved = dict(terminal_send._senders)
    yield
    terminal_send._senders.clear()
    terminal_send._senders.update(saved)


@pytest.fixture
def legacy_plugin_turn(monkeypatch):
    """An attributed plugin turn on a host with no execution-ID binding hook."""
    from clawchat_gateway import host_tools_guard as guard, turn_grants

    session = {}
    nonces = []
    monkeypatch.setattr(guard, "_host_supports_pre_llm_call", lambda: False)
    monkeypatch.setattr(guard, "_session_value", lambda key: session.get(key, ""))
    monkeypatch.setattr(turn_grants, "session_context_value", lambda key: session.get(key, ""))

    def start(*, chat_id, chat_type, user_id, with_snapshot=True):
        nonce = ""
        if with_snapshot:
            snapshot = turn_grants.register_turn(
                account_id="usr_agent", chat_id=chat_id, speakers=(user_id,),
                grants={}, session_key=f"legacy-test:{chat_type}:{chat_id}",
            )
            nonce = snapshot.nonce
            nonces.append(nonce)
        session.update({
            "HERMES_SESSION_PLATFORM": "clawchat",
            "HERMES_SESSION_CHAT_ID": chat_id,
            "HERMES_SESSION_CHAT_TYPE": chat_type,
            "HERMES_SESSION_USER_ID": user_id,
            "HERMES_SESSION_MESSAGE_ID": nonce,
        })
        return nonce

    yield start
    for nonce in nonces:
        turn_grants.release_turn(nonce)
