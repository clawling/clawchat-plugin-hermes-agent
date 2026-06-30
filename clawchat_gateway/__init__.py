"""Public package surface for the ClawChat Hermes gateway adapter.

Do not eagerly import :mod:`clawchat_gateway.adapter` here. The adapter
does ``from gateway.config import Platform`` at module level. Keeping the
import lazy avoids importing Hermes gateway modules before
``ctx.register_platform`` registration has provided the runtime config.
Consumers should import the adapter directly:
``from clawchat_gateway.adapter import ClawChatAdapter``.
"""

# Single source of truth for the package version. pyproject.toml derives its
# build version from this attribute (see [tool.setuptools.dynamic]), and the
# adapter reports it to the ClawChat backend as the plugin version. Bump it HERE on
# release; plugin.yaml's manifest version must be kept in lockstep (guarded by
# tests/test_version_consistency.py).
__version__ = "0.14.0-33"

__all__ = ["__version__"]
