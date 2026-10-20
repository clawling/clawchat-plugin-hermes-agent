"""Owner-facing recovery copy points at the reconnect prompt / wiki page."""

from __future__ import annotations

from clawchat_gateway.activate import ExistingActivationError, UnprovenRepairError
from clawchat_gateway.connection import AUTO_LOGOUT_STATUS_MESSAGE
from clawchat_gateway.onboarding import RECONNECT_GUIDE_URL


def test_auto_logout_message_is_the_cross_plugin_sentence() -> None:
    assert AUTO_LOGOUT_STATUS_MESSAGE == (
        "ClawChat token expired and could not be refreshed. Ask your owner to send you "
        "the reconnect prompt from the ClawChat app, then follow "
        "https://agent-connection.clawling.com/reconnect.md."
    )
    assert "/clawchat-activate" not in AUTO_LOGOUT_STATUS_MESSAGE


def test_existing_activation_error_routes_restore_to_reconnect_page() -> None:
    msg = str(ExistingActivationError("usr_1", "agt_1"))
    assert RECONNECT_GUIDE_URL in msg
    assert "reconnect prompt from the ClawChat app" in msg
    assert "--new-account" in msg
    assert "hermes profile create <name>" in msg


def test_unproven_repair_error_routes_to_reconnect_page() -> None:
    msg = str(UnprovenRepairError("usr_1", "agt_1", profile="work", config_path="config.yaml"))
    assert RECONNECT_GUIDE_URL in msg
    assert "--new-account" in msg
