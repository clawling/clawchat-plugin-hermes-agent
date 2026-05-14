# Gateway Setup - `clawchat_gateway/setup.py`

Interactive setup hook used by Hermes gateway setup after the plugin registers the platform with `setup_fn=_setup_clawchat_platform`.

## `_prompt`

```python
_prompt(default: str, label: str) -> str
```

Prompts with an optional default value and returns the entered value or the default when the user submits a blank line.

## `setup_clawchat_platform`

```python
setup_clawchat_platform() -> None
```

Prompts for:

- ClawChat activation code.
- ClawChat API base URL, defaulting to `DEFAULT_BASE_URL`.

If the activation code is blank, setup prints a skip message and returns without changing config.

When a code is provided, setup calls:

```python
activate_and_maybe_restart(
    code,
    base_url=base_url,
    restart=False,
)
```

`restart=False` is intentional: Hermes gateway setup owns the overall setup session and decides the final service action after all platform setup steps finish. It can restart an already-running gateway, start an installed stopped gateway, or install/start a gateway service when needed. On success, the hook prints the configured user id, base URL, WebSocket URL, and a reminder that Hermes gateway setup will handle that final service step.
