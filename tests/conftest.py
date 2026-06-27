"""Pytest bootstrap: stub Nautobot so the pure source-side modules import.

The unit tests here exercise the Kea source adapter and the value helpers,
neither of which touches Django/Nautobot. But importing the package triggers its
``__init__`` (which subclasses ``NautobotAppConfig``), so we stub just enough of
``nautobot.apps`` for that to succeed. ``diffsync`` is a real dependency and is
NOT stubbed. The Nautobot-side CRUD + the Job are validated end-to-end in the dev
container, not here.
"""

import sys
from unittest.mock import MagicMock

_nautobot = MagicMock()
_nautobot_apps = MagicMock()
# Real (empty) base class so `class X(NautobotAppConfig)` works under the stub.
_nautobot_apps.NautobotAppConfig = type("NautobotAppConfig", (), {})

sys.modules.setdefault("nautobot", _nautobot)
sys.modules["nautobot.apps"] = _nautobot_apps
