"""Fixtures for the `liveware status` probe (tests/ is gitignored here).

Captured from liveware v0.0.33 (commit e431646, built 2026-08-21), the build
served from https://media.clawling.chat/liveware/ at the time of writing:

    $ liveware status
    Liveware agent service status: not_installed.
    $ echo $?
    0

The format string in that binary is `Liveware agent service status: %s.` over
the values not_installed / running / stopped / starting / installed / unknown /
failed, and `status` exits 0 for every one of them — the exit code carries no
signal, so the sentence is the only evidence.

The point of these cases is the ASYMMETRY: a wrong True means no data plane
behind a live app card, a wrong False costs one redundant process. Everything
the parser is not sure about must come out False.
"""
import asyncio

import pytest

from clawchat_gateway.liveware_sample import (
    liveware_agent_is_running,
    parse_agent_status_running,
)

# The real running sentence, per the binary's `... status: %s.` format string.
RUNNING = "Liveware agent service status: running.\n"
# Verbatim capture from `liveware status` on a host with no service installed.
NOT_INSTALLED = "Liveware agent service status: not_installed.\n"


@pytest.mark.parametrize("out", [
    RUNNING,
    RUNNING.replace("\n", "\r\n"),
    "Liveware agent service status: running\n",          # no trailing period
    "LIVEWARE AGENT SERVICE STATUS: RUNNING.\n",
    "  Liveware agent service status: running.  \n",
    f"some preamble\n{RUNNING}trailing\n",               # own line among others
])
def test_running_sentence_is_true(out):
    assert parse_agent_status_running(out) is True


@pytest.mark.parametrize("out", [
    "",
    NOT_INSTALLED,
    "Liveware agent service status: stopped.\n",
    "Liveware agent service status: starting.\n",
    "Liveware agent service status: unknown.\n",
    "Liveware agent service status: failed.\n",
    # every one of these matched the substring regex this first shipped with
    "error: cannot determine status: running or stopped\n",
    "hint: use `liveware status: running` to check\n",
    "app foo status: running-degraded\n",
    "Liveware agent service status: running-degraded.\n",
    "unknown command 'status'\n",
    # `install`'s message, not `status`'s — must not be read as a status line
    "Liveware agent service installed and running.\n",
])
def test_everything_else_is_false(out):
    assert parse_agent_status_running(out) is False


class _Proc:
    def __init__(self, rc, out=b"", err=b""):
        self.returncode, self._out, self._err = rc, out, err

    async def communicate(self):
        return self._out, self._err


async def _awaited(v):
    return v


def _run(**kw):
    return asyncio.run(liveware_agent_is_running(liveware_path="liveware", **kw))


def test_real_not_installed_capture_is_false():
    # exits 0 even when not installed, so only the sentence can say no
    assert _run(exec=lambda *a, **k: _awaited(
        _Proc(0, NOT_INSTALLED.encode()))) is False


def test_real_running_sentence_is_true():
    assert _run(exec=lambda *a, **k: _awaited(
        _Proc(0, RUNNING.encode()))) is True


def test_nonzero_exit_is_false():
    assert _run(exec=lambda *a, **k: _awaited(
        _Proc(1, RUNNING.encode()))) is False


def test_exec_failure_is_false():
    def boom(*a, **k):
        raise FileNotFoundError("no liveware")
    assert _run(exec=boom) is False


def test_stderr_cannot_produce_a_true():
    assert _run(exec=lambda *a, **k: _awaited(
        _Proc(0, b"", RUNNING.encode()))) is False
