"""Make the integration importable with or without Home Assistant installed.

Without Home Assistant, ``custom_components/ev_forecast/__init__.py`` cannot be
imported because it pulls Home Assistant in. The package is therefore
registered as a namespace package here so ``model.py`` can be executed on its
own: the sizing and forecasting rules stay testable with plain pytest.
"""

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ROOT / "custom_components"
INTEGRATION = COMPONENTS / "ev_forecast"

if importlib.util.find_spec("homeassistant") is None:
    for name, path in (
        ("custom_components", COMPONENTS),
        ("custom_components.ev_forecast", INTEGRATION),
    ):
        if name not in sys.modules:
            module = types.ModuleType(name)
            module.__path__ = [str(path)]  # type: ignore[attr-defined]
            sys.modules[name] = module
