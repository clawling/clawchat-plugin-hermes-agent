# Install `clawchat` Plugin From the Local E2E Stage

A short, deterministic guide for installing the ClawChat plugin from the local `/tmp/hermes-clawchat` stage. Follow the steps in order. Each step is one tool:'terminal' call — do not split, do not improvise, do not run additional `hermes` commands beyond what is listed here.

## 1. Verify the Hermes environment

```bash
if [ -d /opt/hermes/.venv/bin ]; then
    echo "hermes exist"
fi
hermes --version
```

If `hermes` is not on the path, source the Hermes venv first.

## 2. Remove any existing installation

```bash
if hermes plugins list | grep -Eq '(^|[[:space:]])clawchat([[:space:]]|$)'; then
    hermes plugins uninstall clawchat
fi
if hermes plugins list | grep -Eq '(^|[[:space:]])hermes-clawchat([[:space:]]|$)'; then
    hermes plugins uninstall hermes-clawchat
fi
```

## 3. Install and enable the plugin from the staged source

```bash
hermes plugins install file:///tmp/hermes-clawchat --enable
```

## 4. Activate ClawChat — one terminal call, then stop

Use the compatibility CLI entrypoint. This works on Hermes v0.12.0, whose top-level `hermes` parser does not expose general plugin CLI commands:

```bash
HERMES_HOME=/opt/data /opt/hermes/.venv/bin/python /opt/data/plugins/clawchat/clawchat_cli.py activate CLAWCHAT_CODE_GOES_HERE
```

Replace `CLAWCHAT_CODE_GOES_HERE` with the activation code the user provided in their original message. The code is one-time-use. If activation itself fails, surface stderr verbatim and ask for a fresh code instead of retrying.

This command writes `CLAWCHAT_TOKEN` and `CLAWCHAT_REFRESH_TOKEN` to `$HERMES_HOME/.env` and writes non-secret ClawChat platform config to `config.yaml`. The same activation path is also available inside Hermes sessions as `/clawchat-activate CODE`. When it exits 0 with:

```text
clawchat: activation complete for <user_id>
```

the install is done. Reply once: "ClawChat is activated." On non-zero exit, reply with stderr verbatim instead.
