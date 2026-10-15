"""Make the pure model module importable without Home Assistant installed.

`custom_components/ev_forecast/__init__.py` pulls in Home Assistant, so the
package is registered as a namespace package here and only `model.py` is
executed. That keeps the sizing-rule tests runnable with plain pytest.
"""

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ROOT / "custom_components"
INTEGRATION = COMPONENTS / "ev_forecast"

for name, path in (("custom_components", COMPONENTS), ("custom_components.ev_forecast", INTEGRATION)):
    if name not in sys.modules:
        module = types.ModuleType(name)
        module.__path__ = [str(path)]  # type: ignore[attr-defined]
        sys.modules[name] = module
