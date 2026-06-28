"""Source adapter: load an ISC Kea DHCPv4 or DHCPv6 config into DiffSync.

This is the read side (PULL). It parses a Kea ``Dhcp4`` or ``Dhcp6`` object and
normalizes every value to its dhcp-models-native form (CIDR prefix, pool range,
MAC/DUID, option data) so the diff against the Nautobot side is apples-to-apples.

Family is detected from the config: a ``subnet6`` key (or an explicit
``family=6``) selects the DHCPv6 path -- subnet6 + pd-pools + prefix-delegation
reservations + the ``dhcp6`` option space. Otherwise the DHCPv4 path runs.

Leases are NOT in the config -- they live in the lease database. To sync them,
pass the parsed memfile lease rows (``kea-leases4.csv`` / ``kea-leases6.csv``)
as ``leases=``; the adapter maps each lease's numeric ``subnet_id`` back to a
CIDR prefix using the config's ``subnet{4,6}[].id``. Kea config has no explicit
exclusion concept, so no exclusions are emitted.
"""

from __future__ import annotations

from diffsync import Adapter
from nautobot_dhcp_models.ssot.base import (
    DhcpDelegatedPrefixReservation,
    DhcpExclusion,
    DhcpLease,
    DhcpOption,
    DhcpPool,
    DhcpPrefixDelegationPool,
    DhcpReservation,
    DhcpScope,
    DhcpServer,
)

from nautobot_ssot_kea.utils.kea import (
    kea_expire_to_iso,
    kea_lease6_type,
    kea_lease_state,
    normalize_mac,
    normalize_option_data,
    parse_kea_pd_pool,
    parse_kea_pool,
    split_kea_prefix,
)

# Kea reservation identifier keys, in preference order. hw-address maps cleanly
# to a MAC; the others are opaque client identifiers passed through normalize_mac
# (which leaves non-MAC values lowercased but otherwise intact).
_RESERVATION_ID_KEYS = ("hw-address", "client-id", "duid", "circuit-id", "flex-id")


def _pd_pool_key(fields: dict) -> str:
    """Build the DiffSync pd_pool identity string from parse_kea_pd_pool() output."""
    return f"{fields['pd_prefix']}/{fields['prefix_length']}-{fields['delegated_length']}"


class KeaAdapter(Adapter):
    """Load a parsed Kea ``Dhcp4``/``Dhcp6`` config dict into the DiffSync store."""

    dhcpserver = DhcpServer
    dhcpscope = DhcpScope
    dhcppool = DhcpPool
    dhcpexclusion = DhcpExclusion
    dhcpreservation = DhcpReservation
    dhcpprefixdelegationpool = DhcpPrefixDelegationPool
    dhcpdelegatedprefixreservation = DhcpDelegatedPrefixReservation
    dhcpoption = DhcpOption
    dhcplease = DhcpLease

    top_level = (
        "dhcpserver",
        "dhcpscope",
        "dhcppool",
        "dhcpexclusion",
        "dhcpreservation",
        "dhcpprefixdelegationpool",
        "dhcpdelegatedprefixreservation",
        "dhcpoption",
        "dhcplease",
    )

    def __init__(
        self,
        *args,
        config: dict,
        server_name: str,
        leases=None,
        family: int | None = None,
        job=None,
        sync=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.config = config
        self.server_name = server_name
        self.leases = leases or []
        # Detect family: explicit arg wins, else a subnet6 key means DHCPv6.
        self.family = family or (6 if "subnet6" in config else 4)
        self.job = job
        self.sync = sync
        # subnet{4,6}[].id -> CIDR prefix, built while loading subnets; lets the
        # lease loader resolve a memfile lease's numeric subnet_id to a prefix.
        self._subnet_id_to_prefix: dict[int, str] = {}

    @property
    def _option_space(self) -> str:
        return "dhcp6" if self.family == 6 else "dhcp4"

    def load(self) -> None:
        """Walk the config: server, global options, then each subnet, then leases."""
        server_name = self.server_name
        if not server_name:
            raise ValueError("KeaAdapter requires a server_name (Kea config has no server identity)")

        # Kea config carries no AD-authorization concept; leave it unknown.
        self.add(self.dhcpserver(name=server_name, vendor="kea", ad_authorized=None))

        for opt in self.config.get("option-data", []):
            self._add_option(server_name, "", "", "", opt)

        global_lifetime = self.config.get("valid-lifetime")
        subnet_key = "subnet6" if self.family == 6 else "subnet4"
        for subnet in self.config.get(subnet_key, []):
            self._load_subnet(server_name, subnet, global_lifetime)

        for lease in self.leases:
            self._load_lease(server_name, lease)

    def _load_subnet(self, server_name: str, subnet: dict, global_lifetime) -> None:
        prefix = subnet["subnet"]  # Kea subnets are already CIDR.
        if subnet.get("id") is not None:
            self._subnet_id_to_prefix[int(subnet["id"])] = prefix

        scope_kwargs = dict(
            server_name=server_name,
            prefix=prefix,
            name="",  # Kea subnets have no name attribute.
            state="enabled",
            default_lease_time=subnet.get("valid-lifetime") or global_lifetime or 86400,
            description=subnet.get("comment") or subnet.get("user-context-description") or "",
        )
        if self.family == 6:
            scope_kwargs.update(
                preferred_lifetime=subnet.get("preferred-lifetime"),
                rapid_commit=subnet.get("rapid-commit"),
                allocator=subnet.get("allocator", ""),
                pd_allocator=subnet.get("pd-allocator", ""),
                relay_addresses=list((subnet.get("relay") or {}).get("ip-addresses", [])),
                interface=subnet.get("interface", ""),
                interface_id=subnet.get("interface-id", ""),
                reservations_in_subnet=subnet.get("reservations-in-subnet"),
                reservations_out_of_pool=subnet.get("reservations-out-of-pool"),
            )
        self.add(self.dhcpscope(**scope_kwargs))

        for pool in subnet.get("pools", []):
            start, end = parse_kea_pool(pool.get("pool", ""))
            self.add(self.dhcppool(server_name=server_name, prefix=prefix, start_address=start, end_address=end))

        for pd_pool in subnet.get("pd-pools", []):
            self._load_pd_pool(server_name, prefix, pd_pool)

        for opt in subnet.get("option-data", []):
            self._add_option(server_name, prefix, "", "", opt)

        for res in subnet.get("reservations", []):
            self._load_reservation(server_name, prefix, res)

    def _load_pd_pool(self, server_name: str, prefix: str, pd_pool: dict) -> None:
        fields = parse_kea_pd_pool(pd_pool)
        self.add(
            self.dhcpprefixdelegationpool(
                server_name=server_name,
                prefix=prefix,
                pd_prefix=fields["pd_prefix"],
                prefix_length=fields["prefix_length"],
                delegated_length=fields["delegated_length"],
                excluded_prefix=fields["excluded_prefix"],
                excluded_prefix_length=fields["excluded_prefix_length"],
                description="",
            )
        )
        for opt in pd_pool.get("option-data", []):
            self._add_option(server_name, prefix, _pd_pool_key(fields), "", opt)

    def _load_reservation(self, server_name: str, prefix: str, res: dict) -> None:
        """Load a reservation; v6 fans ip-addresses[] + prefixes[] out into flat rows."""
        if self.family == 6:
            self._load_v6_reservation(server_name, prefix, res)
            return
        ip = res["ip-address"]
        identifier = ""
        for key in _RESERVATION_ID_KEYS:
            if res.get(key):
                identifier = res[key]
                break
        self.add(
            self.dhcpreservation(
                server_name=server_name,
                prefix=prefix,
                ip_address=ip,
                identifier_type="hw-address",
                mac_address=normalize_mac(identifier),
                hostname=res.get("hostname", ""),
                reservation_type="dhcp",
                description=res.get("comment", ""),
            )
        )
        for opt in res.get("option-data", []):
            self._add_option(server_name, prefix, "", ip, opt)

    def _load_v6_reservation(self, server_name: str, prefix: str, res: dict) -> None:
        """Fan a v6 reservation out: one address row per ip-addresses[], one PD row per prefixes[]."""
        duid = res.get("duid", "")
        hw = res.get("hw-address", "")
        identifier_type = "duid" if duid else ("hw-address" if hw else "flex-id")
        hostname = res.get("hostname", "")
        for ip in res.get("ip-addresses", []):
            self.add(
                self.dhcpreservation(
                    server_name=server_name,
                    prefix=prefix,
                    ip_address=ip,
                    identifier_type=identifier_type,
                    mac_address=normalize_mac(hw) if hw else "",
                    duid=duid,
                    hostname=hostname,
                    reservation_type="dhcp",
                    description=res.get("comment", ""),
                )
            )
        for pd_cidr in res.get("prefixes", []):
            base, length = split_kea_prefix(pd_cidr)
            self.add(
                self.dhcpdelegatedprefixreservation(
                    server_name=server_name,
                    prefix=prefix,
                    delegated_prefix=base,
                    delegated_prefix_length=length,
                    identifier_type=identifier_type,
                    duid=duid,
                    mac_address=normalize_mac(hw) if hw else "",
                    hostname=hostname,
                    description=res.get("comment", ""),
                )
            )

    def _load_lease(self, server_name: str, lease: dict) -> None:
        prefix = self._subnet_id_to_prefix.get(lease["subnet_id"])
        if prefix is None:
            if self.job:
                self.job.logger.warning(
                    f"Skipping lease {lease['address']}: subnet_id {lease['subnet_id']} not in config"
                )
            return
        if self.family == 6:
            self.add(
                self.dhcplease(
                    server_name=server_name,
                    prefix=prefix,
                    ip_address=lease["address"],
                    mac_address=normalize_mac(lease.get("hwaddr", "")) if lease.get("hwaddr") else "",
                    duid=lease.get("duid", ""),
                    hostname=lease.get("hostname", ""),
                    lease_state=kea_lease_state(lease.get("state")),
                    lease_type=kea_lease6_type(lease.get("lease_type")),
                    prefix_length=lease.get("prefix_len"),
                    expires=kea_expire_to_iso(lease.get("expire")),
                )
            )
            return
        identifier = lease.get("hwaddr") or lease.get("client_id") or ""
        self.add(
            self.dhcplease(
                server_name=server_name,
                prefix=prefix,
                ip_address=lease["address"],
                mac_address=normalize_mac(identifier),
                hostname=lease.get("hostname", ""),
                lease_state=kea_lease_state(lease.get("state")),
                expires=kea_expire_to_iso(lease.get("expire")),
            )
        )

    def _add_option(
        self, server_name: str, scope_prefix: str, pd_pool_key: str, reservation_ip: str, opt: dict
    ) -> None:
        self.add(
            self.dhcpoption(
                server_name=server_name,
                scope_prefix=scope_prefix,
                pd_pool_key=pd_pool_key,
                reservation_ip=reservation_ip,
                code=int(opt["code"]),
                value=normalize_option_data(opt.get("data")),
                option_name=opt.get("name", ""),
                # Kea config doesn't carry the option data type; the shared
                # optdef get_or_create resolves it on the target side.
                data_type="string",
                space=self._option_space,
            )
        )
