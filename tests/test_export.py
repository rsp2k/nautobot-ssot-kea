"""Unit tests for the Kea config export's pure logic (pool/exclusion subtraction)."""

from types import SimpleNamespace

from nautobot_ssot_kea.diffsync.adapters.kea import _require_classes
from nautobot_ssot_kea.export import _emit_require_classes, build_cutover_config, pools_minus_exclusions


def test_no_exclusions_passes_through():
    assert pools_minus_exclusions([("10.0.0.10", "10.0.0.250")], []) == [("10.0.0.10", "10.0.0.250")]


def test_single_exclusion_splits_pool():
    assert pools_minus_exclusions([("10.0.0.10", "10.0.0.250")], [("10.0.0.50", "10.0.0.60")]) == [
        ("10.0.0.10", "10.0.0.49"),
        ("10.0.0.61", "10.0.0.250"),
    ]


def test_exclusion_at_start():
    assert pools_minus_exclusions([("10.0.0.10", "10.0.0.250")], [("10.0.0.10", "10.0.0.19")]) == [
        ("10.0.0.20", "10.0.0.250"),
    ]


def test_exclusion_at_end():
    assert pools_minus_exclusions([("10.0.0.10", "10.0.0.250")], [("10.0.0.200", "10.0.0.250")]) == [
        ("10.0.0.10", "10.0.0.199"),
    ]


def test_exclusion_covers_whole_pool():
    assert pools_minus_exclusions([("10.0.0.10", "10.0.0.250")], [("10.0.0.1", "10.0.0.255")]) == []


def test_multiple_exclusions():
    assert pools_minus_exclusions(
        [("10.0.0.10", "10.0.0.250")],
        [("10.0.0.50", "10.0.0.60"), ("10.0.0.100", "10.0.0.110")],
    ) == [
        ("10.0.0.10", "10.0.0.49"),
        ("10.0.0.61", "10.0.0.99"),
        ("10.0.0.111", "10.0.0.250"),
    ]


def test_exclusion_outside_pool_is_ignored():
    assert pools_minus_exclusions([("10.0.0.10", "10.0.0.20")], [("10.0.9.0", "10.0.9.9")]) == [
        ("10.0.0.10", "10.0.0.20"),
    ]


# --- client-class associations: key-aliasing on read, single spelling on write ---


def test_require_classes_reads_legacy_key():
    assert _require_classes({"require-client-classes": ["a", "b"]}) == ["a", "b"]


def test_require_classes_reads_new_key():
    assert _require_classes({"evaluate-additional-classes": ["a"]}) == ["a"]


def test_require_classes_prefers_new_key_when_both_present():
    element = {"require-client-classes": ["old"], "evaluate-additional-classes": ["new"]}
    assert _require_classes(element) == ["new"]


def test_require_classes_absent_is_empty_list():
    assert _require_classes({}) == []


def test_emit_require_classes_writes_legacy_key():
    element = {}
    _emit_require_classes(element, SimpleNamespace(require_client_classes=["corp"]))
    assert element == {"require-client-classes": ["corp"]}


def test_emit_require_classes_omits_when_empty():
    element = {}
    _emit_require_classes(element, SimpleNamespace(require_client_classes=[]))
    assert element == {}  # no empty list emitted -> no diff churn against a bare config


def test_emit_then_read_round_trips():
    element = {}
    _emit_require_classes(element, SimpleNamespace(require_client_classes=["x", "y"]))
    assert _require_classes(element) == ["x", "y"]


# --- migration cutover config builder ---


def _peers():
    return [
        {"name": "node1", "url": "http://172.30.0.11:8000/", "role": "primary"},
        {"name": "node2", "url": "http://172.30.0.12:8000/", "role": "standby"},
    ]


def test_cutover_preserves_node_plumbing_and_injects_ha():
    generated = {"Dhcp4": {"subnet4": [{"id": 1, "subnet": "10.0.10.0/24"}]}}
    current = {
        "Dhcp4": {
            "control-socket": {"socket-type": "unix", "socket-name": "/run/kea/kea4-ctrl-socket"},
            "interfaces-config": {"interfaces": ["eth0"]},
            "lease-database": {"type": "memfile"},
            "subnet4": [{"id": 9, "subnet": "192.0.2.0/24"}],
        }
    }
    out = build_cutover_config(generated, current, this_server_name="node1", peers=_peers(), mode="hot-standby")[
        "Dhcp4"
    ]
    # Generated DHCP content wins...
    assert [s["subnet"] for s in out["subnet4"]] == ["10.0.10.0/24"]
    # ...but the node's plumbing (incl. the control socket the CA needs) is preserved.
    assert out["control-socket"]["socket-name"] == "/run/kea/kea4-ctrl-socket"
    assert out["interfaces-config"] == {"interfaces": ["eth0"]}
    assert out["lease-database"] == {"type": "memfile"}
    # HA + lease_cmds hooks installed with this server's name.
    libs = [h["library"] for h in out["hooks-libraries"]]
    assert any("libdhcp_lease_cmds.so" in lib for lib in libs)
    ha = [h for h in out["hooks-libraries"] if "libdhcp_ha.so" in h["library"]][0]
    rel = ha["parameters"]["high-availability"][0]
    assert rel["this-server-name"] == "node1"
    assert rel["mode"] == "hot-standby"
    assert [p["name"] for p in rel["peers"]] == ["node1", "node2"]


def test_cutover_replaces_existing_ha_not_duplicates():
    # A node that already had an HA hook shouldn't end up with two.
    generated = {"Dhcp4": {"subnet4": []}}
    current = {
        "Dhcp4": {
            "control-socket": {"socket-name": "/run/kea/kea4-ctrl-socket"},
            "hooks-libraries": [
                {"library": "/x/libdhcp_ha.so", "parameters": {"high-availability": [{"this-server-name": "old"}]}},
                {"library": "/x/libdhcp_lease_cmds.so"},
            ],
        }
    }
    out = build_cutover_config(generated, current, this_server_name="node2", peers=_peers(), mode="hot-standby")[
        "Dhcp4"
    ]
    ha_hooks = [h for h in out["hooks-libraries"] if "libdhcp_ha.so" in h["library"]]
    lease_hooks = [h for h in out["hooks-libraries"] if "libdhcp_lease_cmds.so" in h["library"]]
    assert len(ha_hooks) == 1 and len(lease_hooks) == 1
    assert ha_hooks[0]["parameters"]["high-availability"][0]["this-server-name"] == "node2"
