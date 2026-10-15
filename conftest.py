"""Root conftest: registers the Home Assistant test plugin when it is installed.

The pure-model tests run with plain pytest; the runtime tests need
pytest-homeassistant-custom-component, which is only available in an
environment that has Home Assistant installed.
"""

import importlib.util

if importlib.util.find_spec("pytest_homeassistant_custom_component") is not None:
    pytest_plugins = ["pytest_homeassistant_custom_component"]
