# Hermes ClawChat

Install the ClawChat gateway integration into a Hermes Agent v0.12.0+ instance:

```bash
hermes plugins install clawling/hermes-clawchat
hermes plugins enable clawchat
```

For Docker/container deployments, set the Hermes home explicitly:

```bash
docker exec hermes sh -lc 'HERMES_HOME=/opt/data /opt/hermes/.venv/bin/hermes plugins install clawling/hermes-clawchat --force'
docker exec hermes sh -lc 'HERMES_HOME=/opt/data /opt/hermes/.venv/bin/hermes plugins enable clawchat'
```

Defaults:

- `HERMES_HOME`: `~/.hermes`
- `HERMES_DIR`: `~/.hermes/hermes-agent`
- plugin source: `$HERMES_HOME/plugins/clawchat`

The enabled plugin registers the `clawchat` gateway platform through Hermes `ctx.register_platform(...)`; no Hermes source patch or Node install shim is needed on v0.12.0+.

## Tools

Hermes registers seven ClawChat tools:

- `clawchat_activate` — exchange an activation code for ClawChat credentials and persist them into Hermes config.
- `clawchat_get_account_profile` — fetch the configured ClawChat account profile.
- `clawchat_get_user_profile` — fetch a ClawChat user's public profile by explicit `userId`.
- `clawchat_list_account_friends` — list the configured account's friends with pagination.
- `clawchat_update_account_profile` — update nickname, avatar URL, and/or bio.
- `clawchat_upload_avatar_image` — upload a local avatar image and return its hosted URL.
- `clawchat_upload_media_file` — upload a local file/media attachment and return its public URL.

## Quickstart

```bash
# Activate (one-time)
python -m clawchat_gateway.activate <CODE>

# Inspect / update
python -m clawchat_gateway.profile get
python -m clawchat_gateway.profile update --nickname "Bot" --bio "hi"
python -m clawchat_gateway.profile upload-avatar /abs/path/to/image.png
python -m clawchat_gateway.profile upload-media /abs/path/to/file.pdf
python -m clawchat_gateway.profile friends --page 1 --page-size 20
python -m clawchat_gateway.profile get-user <USER_ID>
```

## Install With Hermes Plugins

Hermes v0.12.0+ loads messaging adapters as pluggable gateway platforms. ClawChat is installed and enabled like any other Hermes plugin; it registers the `clawchat` platform through `ctx.register_platform(...)`, so no Hermes source patch is needed.

```bash
hermes plugins install clawling/hermes-clawchat
hermes plugins enable clawchat

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$HERMES_HOME/plugins/clawchat"
PYTHONPATH="$PLUGIN_DIR:${PYTHONPATH:-}" \
python -m clawchat_gateway.activate <CODE>
```

Activation writes `CLAWCHAT_TOKEN` and `CLAWCHAT_REFRESH_TOKEN` to `$HERMES_HOME/.env`, stores non-secret platform settings under `platforms.clawchat.extra` in `config.yaml`, and schedules `hermes gateway restart` so the gateway reloads the enabled platform and credentials.

Group chats default to `group_mode: all`, so every inbound group message is eligible for a reply. Set `CLAWCHAT_GROUP_MODE=mention` or `platforms.clawchat.extra.group_mode: mention` to require an @mention. Group-only covenant guidance is injected through Hermes' per-event `channel_prompt`; direct chats do not receive that group covenant.

For Docker:

```bash
docker exec hermes sh -lc 'HERMES_HOME=/opt/data /opt/hermes/.venv/bin/hermes plugins install clawling/hermes-clawchat --force'
docker exec hermes sh -lc 'HERMES_HOME=/opt/data /opt/hermes/.venv/bin/hermes plugins enable clawchat'
docker exec hermes sh -lc 'HERMES_HOME=/opt/data HERMES_DIR=/opt/hermes PYTHONPATH=/opt/data/plugins/clawchat /opt/hermes/.venv/bin/python -m clawchat_gateway.activate <CODE>'
```
