"""Read YAML with the host's parser: hermes_yaml, then PyYAML, then ruamel.

New Hermes uses hermes_yaml; older Hermes uses PyYAML even when ruamel is
installed. The ruamel fallback follows hermes_yaml's YAML 1.1 policy, with
a fresh parser per read and a pure-parser retry after a C-parser rejection.
"""

from __future__ import annotations

import importlib
import logging
import sys
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

_backend: tuple[str, Callable[[str | bytes], Any]] | None = None
_warned_unavailable = False


class YamlUnavailableError(RuntimeError):
    """No usable YAML reader is available in the Hermes Python environment."""


def _resolve_backend() -> tuple[str, Callable[[str | bytes], Any]]:
    global _backend, _warned_unavailable
    if _backend is not None:
        return _backend

    for module_name, name in (("hermes_yaml", "hermes_yaml"), ("yaml", "pyyaml")):
        try:
            loader = importlib.import_module(module_name).safe_load
        except Exception:
            logger.debug("ClawChat YAML reader %s unavailable", module_name, exc_info=True)
            continue
        _backend = (name, loader)
        return _backend

    try:
        yaml_class = importlib.import_module("ruamel.yaml").YAML
        yaml_error = importlib.import_module("ruamel.yaml.error").YAMLError

        def load_ruamel(document: str | bytes) -> Any:
            def load(*, pure: bool) -> Any:
                parser = yaml_class(typ="safe", pure=pure)
                parser.version = (1, 1)
                return parser.load(document)

            try:
                return load(pure=False)
            except yaml_error:
                return load(pure=True)

    except Exception:
        logger.debug("ClawChat YAML reader ruamel.yaml unavailable", exc_info=True)
    else:
        _backend = ("ruamel", load_ruamel)
        return _backend

    message = (
        "ClawChat needs hermes_yaml, PyYAML or ruamel.yaml to read Hermes YAML files; "
        f"none can be imported in the current Python ({sys.executable}). "
        "Please repair or upgrade Hermes Agent, or install PyYAML in Hermes' Python."
    )
    if not _warned_unavailable:
        _warned_unavailable = True
        logger.warning(message)
    raise YamlUnavailableError(message)


def safe_load(stream: str | bytes) -> Any:
    """Read YAML using the first available host-compatible backend."""
    return _resolve_backend()[1](stream)


def backend_name() -> str:
    """Return the selected reader's name, resolving it on the first call."""
    return _resolve_backend()[0]


def _reset_backend_for_tests() -> None:
    global _backend, _warned_unavailable
    _backend = None
    _warned_unavailable = False
