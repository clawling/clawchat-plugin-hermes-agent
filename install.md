# Install `hermes-clawchat` Plugin

A short, deterministic guide for installing the ClawChat plugin into a Hermes Agent environment. Follow the steps in order. Each step is one tool:'terminal' call — do not split, do not improvise, do not run additional `hermes` commands beyond what is listed here.

> Human operators: use [`docs/install.md`](docs/install.md) instead. This
> file is the runbook consumed by LLM installers (e.g. the
> `@newbase-clawchat/clawchat-cli` install prompt) and intentionally stays
> rigid.

## 1. Verify the Hermes environment

```bash
if [ -d /opt/hermes/.venv/bin ]; then
    echo "hermes exist"
fi
hermes --version
```

If `hermes` is not on the path, source the Hermes venv first.

## 2. Remove any existing installation

If `clawchat` is already installed, uninstall it first:

```bash
hermes plugins list | grep clawchat
hermes plugins uninstall clawchat   # only if listed
```

## 3. Install and enable the plugin

```bash
hermes plugins install clawling/clawchat-plugin-hermes-agent
hermes plugins enable clawchat
```

On Hermes v0.12.0 and newer this loads ClawChat as a pluggable gateway platform. The plugin calls `ctx.register_platform(...)` at startup, so the ClawChat adapter is recognized by the gateway without patching Hermes source files. This also registers the twenty-two account/profile/media/search/moment/read-only conversation and file-backed memory/metadata `clawchat_*` tools (see `plugin.yaml` for the authoritative list) and copies the plugin source into `$HERMES_HOME/plugins/clawchat/`.

This install step does not ask for `CLAWCHAT_TOKEN` or `CLAWCHAT_REFRESH_TOKEN`; those credentials are created only by the activation step below.

## 4. Activate ClawChat — one terminal call, then stop

Use the command that matches the Hermes version from step 1.

Hermes v0.12.0:

```bash
python "${HERMES_HOME:-$HOME/.hermes}/plugins/clawchat/clawchat_cli.py" activate CLAWCHAT_CODE_GOES_HERE
```

Hermes versions newer than v0.12.0:

```bash
hermes clawchat activate CLAWCHAT_CODE_GOES_HERE
```

Replace `CLAWCHAT_CODE_GOES_HERE` with the activation code the user provided in their original message. The code is one-time-use. If activation itself fails, surface stderr verbatim and ask for a fresh code instead of retrying.

The activation command writes `CLAWCHAT_TOKEN` and `CLAWCHAT_REFRESH_TOKEN` to `$HERMES_HOME/.env` and writes non-secret ClawChat platform config to `config.yaml`. When activation exits 0 with:

```text
clawchat: activation complete for <user_id>
```

the install is done. Reply once: "ClawChat is activated." On non-zero exit, reply with stderr verbatim instead.
