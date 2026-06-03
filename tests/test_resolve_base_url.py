from __future__ import annotations

from clawchat_gateway.api_client import DEFAULT_BASE_URL
from clawchat_gateway.config import resolve_activation_base_url


def test_env_base_url_takes_precedence(tmp_path, monkeypatch):
    # Isolate from any real ~/.hermes/.env by pointing HERMES_HOME at an empty dir.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CLAWCHAT_BASE_URL", "https://company.newbaselab.com:39001")
    assert resolve_activation_base_url() == "https://company.newbaselab.com:39001"


def test_falls_back_to_default_when_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("CLAWCHAT_BASE_URL", raising=False)
    assert resolve_activation_base_url() == DEFAULT_BASE_URL


def test_reads_base_url_from_hermes_env_file(tmp_path, monkeypatch):
    # The installer writes CLAWCHAT_BASE_URL into $HERMES_HOME/.env; activation
    # must pick it up even when it is not exported into the process env.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("CLAWCHAT_BASE_URL", raising=False)
    (tmp_path / ".env").write_text("CLAWCHAT_BASE_URL=https://company.newbaselab.com:39001\n")
    assert resolve_activation_base_url() == "https://company.newbaselab.com:39001"
