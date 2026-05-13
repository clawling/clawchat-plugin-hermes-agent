import sys
import types

from clawchat_gateway.config import ClawChatConfig


def test_config_defaults():
    cfg = ClawChatConfig.from_platform_config(
        type(
            "PC",
            (),
            {
                "extra": {
                    "websocket_url": "wss://chat.example/ws",
                    "token": "tok",
                    "refresh_token": "rt",
                    "user_id": "u1",
                }
            },
        )()
    )
    assert cfg.websocket_url == "wss://chat.example/ws"
    assert cfg.base_url == ""
    assert cfg.token == "tok"
    assert cfg.refresh_token == "rt"
    assert cfg.user_id == "u1"
    assert cfg.reply_mode == "stream"
    assert cfg.group_mode == "all"
    assert cfg.stream_flush_interval_ms == 250
    assert cfg.stream_min_chunk_chars == 40
    assert cfg.stream_max_buffer_chars == 2000
    assert cfg.reconnect_initial_delay_ms == 500
    assert cfg.reconnect_max_delay_ms == 15000
    assert cfg.reconnect_jitter_ratio == 0.3
    assert cfg.reconnect_max_retries == float("inf")
    assert cfg.heartbeat_interval_ms == 20000
    assert cfg.heartbeat_timeout_ms == 10000
    assert cfg.ack_timeout_ms == 15000
    assert cfg.ack_auto_resend_on_timeout is False
    assert cfg.media_local_roots == ()
    assert cfg.show_tools_output is False
    assert cfg.show_tool_progress is False
    assert cfg.show_think_output is False
    assert cfg.enable_rich_interactions is False


def test_config_reads_snake_case_hermes_extra_keys():
    cfg = ClawChatConfig.from_platform_config(
        type(
            "PC",
            (),
            {
                "extra": {
                    "websocket_url": "wss://chat.example/ws",
                    "base_url": "https://api.example",
                    "token": "tok",
                    "refresh_token": "rt",
                    "user_id": "u1",
                    "reply_mode": "stream",
                    "group_mode": "all",
                    "reconnect_initial_delay_ms": 111,
                    "reconnect_max_delay_ms": 222,
                    "reconnect_jitter_ratio": 0.12,
                    "reconnect_max_retries": 7,
                    "heartbeat_interval_ms": 333,
                    "heartbeat_timeout_ms": 444,
                    "ack_timeout_ms": 555,
                    "ack_auto_resend_on_timeout": True,
                    "media_local_roots": ["/tmp/a", "/tmp/b"],
                    "show_tools_output": True,
                    "show_tool_progress": False,
                    "show_think_output": True,
                    "enable_rich_interactions": True,
                    "stream": {
                        "flush_interval_ms": 100,
                        "min_chunk_chars": 8,
                        "max_buffer_chars": 512,
                    },
                }
            },
        )()
    )
    assert cfg.websocket_url == "wss://chat.example/ws"
    assert cfg.base_url == "https://api.example"
    assert cfg.token == "tok"
    assert cfg.refresh_token == "rt"
    assert cfg.user_id == "u1"
    assert cfg.reply_mode == "stream"
    assert cfg.group_mode == "all"
    assert cfg.reconnect_initial_delay_ms == 111
    assert cfg.reconnect_max_delay_ms == 222
    assert cfg.reconnect_jitter_ratio == 0.12
    assert cfg.reconnect_max_retries == 7
    assert cfg.heartbeat_interval_ms == 333
    assert cfg.heartbeat_timeout_ms == 444
    assert cfg.ack_timeout_ms == 555
    assert cfg.ack_auto_resend_on_timeout is True
    assert cfg.media_local_roots == ("/tmp/a", "/tmp/b")
    assert cfg.show_tools_output is True
    assert cfg.show_tool_progress is False
    assert cfg.show_think_output is True
    assert cfg.enable_rich_interactions is True
    assert cfg.stream_flush_interval_ms == 100
    assert cfg.stream_min_chunk_chars == 8
    assert cfg.stream_max_buffer_chars == 512


def test_config_ignores_openclaw_camel_case_extra_keys():
    cfg = ClawChatConfig.from_platform_config(
        type(
            "PC",
            (),
            {
                "extra": {
                    "websocketUrl": "wss://chat.example/ws",
                    "baseUrl": "https://api.example",
                    "token": "tok",
                    "refreshToken": "rt",
                    "userId": "u1",
                    "replyMode": "static",
                    "groupMode": "all",
                    "reconnectInitialDelayMs": 111,
                    "reconnectMaxDelayMs": 222,
                    "reconnectJitterRatio": 0.12,
                    "reconnectMaxRetries": 7,
                    "heartbeatIntervalMs": 333,
                    "heartbeatTimeoutMs": 444,
                    "ackTimeoutMs": 555,
                    "ackAutoResendOnTimeout": True,
                    "mediaLocalRoots": ["/tmp/a", "/tmp/b"],
                    "mediaDownloadDir": "/tmp/custom-media",
                    "showToolsOutput": True,
                    "showToolProgress": True,
                    "showThinkOutput": True,
                    "enableRichInteractions": True,
                    "stream": {
                        "flushIntervalMs": 100,
                        "minChunkChars": 8,
                        "maxBufferChars": 512,
                    },
                }
            },
        )()
    )

    assert cfg.websocket_url == ""
    assert cfg.base_url == ""
    assert cfg.token == "tok"
    assert cfg.refresh_token == ""
    assert cfg.user_id == ""
    assert cfg.reply_mode == "stream"
    assert cfg.group_mode == "all"
    assert cfg.reconnect_initial_delay_ms == 500
    assert cfg.reconnect_max_delay_ms == 15000
    assert cfg.reconnect_jitter_ratio == 0.3
    assert cfg.reconnect_max_retries == float("inf")
    assert cfg.heartbeat_interval_ms == 20000
    assert cfg.heartbeat_timeout_ms == 10000
    assert cfg.ack_timeout_ms == 15000
    assert cfg.ack_auto_resend_on_timeout is False
    assert cfg.media_local_roots == ()
    assert cfg.media_download_dir == "/tmp/clawchat-media"
    assert cfg.show_tools_output is False
    assert cfg.show_tool_progress is False
    assert cfg.show_think_output is False
    assert cfg.enable_rich_interactions is False
    assert cfg.stream_flush_interval_ms == 250
    assert cfg.stream_min_chunk_chars == 40
    assert cfg.stream_max_buffer_chars == 2000


def test_config_reads_connection_credentials_from_env(monkeypatch):
    monkeypatch.setenv("CLAWCHAT_WEBSOCKET_URL", "wss://env.example/ws")
    monkeypatch.setenv("CLAWCHAT_BASE_URL", "https://env.example")
    monkeypatch.setenv("CLAWCHAT_TOKEN", "env-token")
    monkeypatch.setenv("CLAWCHAT_REFRESH_TOKEN", "env-refresh-token")
    monkeypatch.setenv("CLAWCHAT_USER_ID", "env-user")

    cfg = ClawChatConfig.from_platform_config(
        type(
            "PC",
            (),
            {
                "extra": {
                    "websocket_url": "wss://config.example/ws",
                    "base_url": "https://config.example",
                    "token": "config-token",
                    "user_id": "config-user",
                }
            },
        )()
    )

    assert cfg.websocket_url == "wss://env.example/ws"
    assert cfg.base_url == "https://env.example"
    assert cfg.token == "env-token"
    assert cfg.refresh_token == "env-refresh-token"
    assert cfg.user_id == "env-user"


def test_config_reads_connection_credentials_from_hermes_env_file(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / ".env").write_text(
        "\n".join(
            [
                "CLAWCHAT_TOKEN=file-token",
                "CLAWCHAT_REFRESH_TOKEN=file-refresh-token",
                "export CLAWCHAT_USER_ID=file-user",
                "CLAWCHAT_BASE_URL='https://file.example'",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    cfg = ClawChatConfig.from_platform_config(
        type(
            "PC",
            (),
            {
                "extra": {
                    "websocket_url": "wss://config.example/ws",
                    "token": "config-token",
                    "user_id": "config-user",
                }
            },
        )()
    )

    assert cfg.websocket_url == "wss://config.example/ws"
    assert cfg.base_url == "https://file.example"
    assert cfg.token == "file-token"
    assert cfg.refresh_token == "file-refresh-token"
    assert cfg.user_id == "file-user"


def test_config_prefers_hermes_env_api(monkeypatch):
    hermes_cli_module = types.ModuleType("hermes_cli")
    hermes_config_module = types.ModuleType("hermes_cli.config")
    hermes_config_module.get_env_value = {
        "CLAWCHAT_TOKEN": "api-token",
        "CLAWCHAT_REFRESH_TOKEN": "api-refresh-token",
        "CLAWCHAT_USER_ID": "api-user",
    }.get
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_module)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", hermes_config_module)

    cfg = ClawChatConfig.from_platform_config(
        type(
            "PC",
            (),
            {
                "extra": {
                    "websocket_url": "wss://config.example/ws",
                    "token": "config-token",
                    "user_id": "config-user",
                }
            },
        )()
    )

    assert cfg.websocket_url == "wss://config.example/ws"
    assert cfg.token == "api-token"
    assert cfg.refresh_token == "api-refresh-token"
    assert cfg.user_id == "api-user"


def test_config_keeps_tool_progress_compatible_with_tools_output_when_omitted():
    cfg = ClawChatConfig.from_platform_config(
        type(
            "PC",
            (),
            {
                "extra": {
                    "websocket_url": "wss://chat.example/ws",
                    "token": "tok",
                    "show_tools_output": True,
                }
            },
        )()
    )

    assert cfg.show_tools_output is True
    assert cfg.show_tool_progress is True
