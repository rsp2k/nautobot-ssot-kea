"""Nautobot config for the SSoT ISC Kea DHCP dev stack."""

import os

from nautobot.core.settings import *  # noqa: F401,F403
from nautobot.core.settings_funcs import is_truthy  # noqa: F401

DEBUG = is_truthy(os.environ.get("NAUTOBOT_DEBUG", "true"))

PLUGINS = [
    "nautobot_ssot",
    "nautobot_dhcp_models",
    "nautobot_ssot_kea",
]

PLUGINS_CONFIG = {
    "nautobot_ssot": {
        "hide_example_jobs": True,
    },
}
