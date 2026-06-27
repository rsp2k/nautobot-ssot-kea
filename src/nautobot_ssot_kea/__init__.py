"""Nautobot SSoT data source for ISC Kea DHCP.

Reads an ISC Kea DHCPv4 configuration (``kea-dhcp4.conf``) and syncs it one-way
into ``nautobot-dhcp-models``. Config-only in v1: subnets, pools, reservations,
and options. Kea leases live in the lease database, not the config, so they are
out of scope here (see the adapter module docstring).
"""

from importlib.metadata import PackageNotFoundError, version

from nautobot.apps import NautobotAppConfig

try:
    __version__ = version("nautobot-ssot-kea")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0+unknown"


class NautobotSSoTKeaConfig(NautobotAppConfig):
    """App configuration for the ISC Kea DHCP SSoT integration."""

    name = "nautobot_ssot_kea"
    verbose_name = "Nautobot SSoT ISC Kea"
    description = "Sync an ISC Kea DHCPv4 config (subnets, pools, reservations, options) into nautobot-dhcp-models."
    version = __version__
    author = "Ryan Malloy"
    author_email = "ryan@supported.systems"
    base_url = "ssot-kea"
    required_settings: list[str] = []
    default_settings: dict = {}
    caching_config: dict = {}


config = NautobotSSoTKeaConfig
