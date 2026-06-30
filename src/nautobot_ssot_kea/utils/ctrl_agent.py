"""Client for the ISC Kea Control Agent REST API.

The Control Agent (``kea-ctrl-agent``) exposes an HTTP endpoint that accepts JSON
commands and proxies them to the dhcp4/dhcp6/d2 daemons. This wraps the handful we
need for sync + migration: pull the running config and leases (import), and push a
new config (the MS->Kea cutover).

The HTTP call sits behind ``_post`` so unit tests can inject a fake transport
without ``requests`` installed; at runtime ``requests`` (a Nautobot dependency) is
imported lazily.

Kea command result codes: 0 success, 1 error, 2 unsupported, 3 empty (success, no
data). We raise on 1/2 and treat 0/3 as success.
"""

from __future__ import annotations


class KeaCommandError(Exception):
    """A Kea Control Agent command returned an error (result 1) or was unsupported (2)."""


class KeaControlAgent:
    """Thin client for one Kea Control Agent endpoint."""

    def __init__(
        self,
        base_url: str,
        *,
        username: str | None = None,
        password: str | None = None,
        verify: bool = True,
        timeout: int = 30,
        post=None,
    ):
        """Configure the endpoint URL, optional basic-auth creds, and TLS/timeout."""
        self.base_url = base_url.rstrip("/") + "/"
        self.username = username
        self.password = password
        self.verify = verify
        self.timeout = timeout
        self._post = post or self._default_post

    def _default_post(self, payload: dict):
        """POST the command payload to the CA and return the decoded JSON."""
        import requests

        auth = (self.username, self.password) if self.username else None
        resp = requests.post(self.base_url, json=payload, auth=auth, verify=self.verify, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def command(self, command: str, service=("dhcp4",), arguments: dict | None = None) -> dict:
        """Run one CA command; return its result dict. Raises KeaCommandError on failure.

        The CA returns a list with one entry per addressed service; we address a
        single service and return that entry.
        """
        payload: dict = {"command": command}
        if service:
            payload["service"] = list(service)
        if arguments is not None:
            payload["arguments"] = arguments

        data = self._post(payload)
        result = data[0] if isinstance(data, list) else data
        code = result.get("result", 1)
        if code in (1, 2):
            kind = "unsupported" if code == 2 else "error"
            raise KeaCommandError(f"Kea '{command}' {kind}: {result.get('text', 'no detail')}")
        return result

    # --- Read (import) -------------------------------------------------------

    def config_get(self, service: str = "dhcp4") -> dict:
        """Return the running config, e.g. ``{'Dhcp4': {...}}``."""
        return self.command("config-get", service=[service]).get("arguments", {})

    def leases_get_all(self, family: int = 4) -> list:
        """Return all leases (empty list if none / lease_cmds gives result 3)."""
        cmd = "lease6-get-all" if family == 6 else "lease4-get-all"
        result = self.command(cmd, service=[f"dhcp{family}"])
        return result.get("arguments", {}).get("leases", [])

    def status_get(self, service: str = "dhcp4") -> dict:
        """Daemon status (pid, uptime, HA servers...). A cheap connectivity preflight."""
        return self.command("status-get", service=[service]).get("arguments", {})

    def ha_heartbeat(self, service: str = "dhcp4") -> dict:
        """HA state for this server (state name, date, etc.)."""
        return self.command("ha-heartbeat", service=[service]).get("arguments", {})

    def list_commands(self, service: str = "dhcp4") -> list:
        """Commands this daemon supports (reveals which hooks are loaded)."""
        return self.command("list-commands", service=[service]).get("arguments", [])

    # --- Write (cutover) -----------------------------------------------------

    def config_set(self, config: dict, service: str = "dhcp4") -> dict:
        """Apply a full config in memory (takes effect immediately, not persisted).

        ``config`` is the wrapped object, e.g. ``{'Dhcp4': {...}}``.
        """
        return self.command("config-set", service=[service], arguments=config)

    def config_write(self, filename: str | None = None, service: str = "dhcp4") -> dict:
        """Persist the running config to disk (defaults to the loaded config file)."""
        arguments = {"filename": filename} if filename else None
        return self.command("config-write", service=[service], arguments=arguments)
