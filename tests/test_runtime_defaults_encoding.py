"""The plugin-load ``.env`` rewrite must not depend on the OS locale.

Hermes writes ``.env`` as UTF-8. On a Windows machine whose ANSI code page is
not UTF-8 (e.g. GBK), a locale-default read either raises or mis-decodes any
non-ASCII line, and the write-back then re-encodes the mojibake.
"""

from __future__ import annotations

import io
import tomllib
from pathlib import Path

from clawchat_gateway.runtime_defaults import configure_clawchat_allow_all

REPO = Path(__file__).resolve().parent.parent


def _force_locale_encoding(monkeypatch, encoding: str) -> None:
    real_open = io.open

    def open_with_locale(file, mode="r", buffering=-1, encoding_=None, *args, **kwargs):
        if "encoding" in kwargs:
            encoding_ = kwargs.pop("encoding")
        if "b" not in mode and encoding_ in (None, "locale"):
            encoding_ = encoding
        return real_open(file, mode, buffering, encoding_, *args, **kwargs)

    monkeypatch.setattr(io, "open", open_with_locale)


def test_env_round_trips_utf8_under_non_utf8_locale(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    env = tmp_path / ".env"
    original = "# Café key €\nOPENAI_API_KEY=sk-test\n"
    env.write_bytes(original.encode("utf-8"))
    _force_locale_encoding(monkeypatch, "gbk")

    assert configure_clawchat_allow_all() is True

    text = env.read_bytes().decode("utf-8")
    assert text == original + "CLAWCHAT_ALLOW_ALL_USERS=true\n"


def test_websockets_floor_has_asyncio_client():
    # connection.py imports websockets.asyncio.client, which first shipped in 13.0.
    deps = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    spec = next(d for d in deps if d.startswith("websockets"))
    floor = spec.split(">=", 1)[1].split(",", 1)[0]
    assert int(floor.split(".")[0]) >= 13, spec
