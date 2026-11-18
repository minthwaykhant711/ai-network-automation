"""Offline tests (no routers, no Kafka, no AI): template output, validation rules, post-processing.

    .venv/bin/python -m pytest -q tests      (or: .venv/bin/python tests/test_offline.py)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ai import postprocess  # noqa: E402
from app.config import RouterInventory  # noqa: E402
from app.ios import config_digest, render_plan, rollback_commands  # noqa: E402
from app.models import TopologyPlan  # noqa: E402
from app.validation import static_checks  # noqa: E402

INV = {
    "R1": RouterInventory("R1", "192.0.2.1", "FastEthernet0/0", ["FastEthernet1/0"], ["FastEthernet1/1"], "1.1.1.1"),
    "R2": RouterInventory("R2", "192.0.2.2", "FastEthernet0/0", ["FastEthernet1/0"], ["FastEthernet1/1"], "2.2.2.2"),
}

EXAM = {
    "routers": [
        {"name": "R1", "router_id": "1.1.1.1", "interfaces": [
            {"name": "FastEthernet1/0", "role": "lan", "ipv4_cidr": "10.0.0.1/24"},
            {"name": "FastEthernet1/1", "role": "transit", "ipv4_cidr": "10.1.0.1/24"},
            {"name": "FastEthernet0/0", "role": "management", "ipv4_cidr": None}]},
        {"name": "R2", "router_id": "2.2.2.2", "interfaces": [
            {"name": "FastEthernet1/1", "role": "transit", "ipv4_cidr": "10.1.0.2/24"},
            {"name": "FastEthernet1/0", "role": "lan", "ipv4_cidr": "10.2.0.2/24"},
            {"name": "FastEthernet0/0", "role": "management", "ipv4_cidr": None}]},
    ],
    "pcs": [{"name": "PC1", "ipv4_cidr": "10.0.0.10/24", "gateway": "10.0.0.1"},
            {"name": "PC2", "ipv4_cidr": "10.2.0.10/24", "gateway": "10.2.0.2"}],
    "links": [{"device_a": "R1", "interface_a": "FastEthernet1/1", "device_b": "R2", "interface_b": "FastEthernet1/1"}],
    "ospf_process_id": 1, "ospf_area": "0", "ambiguities": [],
}

R1_EXPECTED = """configure terminal
no router ospf 1
interface FastEthernet1/0
ip address 10.0.0.1 255.255.255.0
no shutdown
exit
interface FastEthernet1/1
ip address 10.1.0.1 255.255.255.0
no shutdown
exit
router ospf 1
router-id 1.1.1.1
passive-interface FastEthernet1/0
no passive-interface FastEthernet1/1
network 10.0.0.0 0.0.0.255 area 0
network 10.1.0.0 0.0.0.255 area 0
end""".splitlines()


def plan(**over):
    import copy
    d = copy.deepcopy(EXAM)
    d.update(over)
    return TopologyPlan.model_validate(d)


def test_exam_template_output():
    cmds, errors = render_plan(plan(), INV)
    assert not errors
    assert cmds["R1"] == R1_EXPECTED
    assert "passive-interface FastEthernet1/0" in cmds["R2"]
    assert "network 10.2.0.0 0.0.0.255 area 0" in cmds["R2"]
    assert not any("FastEthernet0/0" in ln for c in cmds.values() for ln in c)
    assert len(config_digest(cmds)) == 64


def test_exam_validates():
    errors, warnings = static_checks(plan(), INV)
    assert errors == [], errors


def test_management_interface_rejected():
    p = plan()
    p.routers[0].interfaces[0].name = "FastEthernet0/0"
    errors, _ = static_checks(p, INV)
    assert any("management" in e for e in errors)


def test_open_ambiguity_blocks():
    errors, _ = static_checks(plan(ambiguities=["R2 LAN prefix unreadable"]), INV)
    assert any("unresolved" in e for e in errors)


def test_transit_mismatch_rejected():
    p = plan()
    p.routers[1].interfaces[0].ipv4_cidr = "10.9.0.2/24"
    errors, _ = static_checks(p, INV)
    assert any("Transit subnet" in e for e in errors)


def test_no_pcs_is_fine():
    errors, _ = static_checks(plan(pcs=[]), INV)
    assert errors == []


def test_bad_gateway():
    p = plan()
    p.pcs[0].gateway = "10.0.0.254"
    errors, _ = static_checks(p, INV)
    assert any("gateway" in e for e in errors)


def test_postprocess_fills_names_and_flags_mgmt_label():
    raw = TopologyPlan.model_validate({
        "routers": [
            {"name": "router1", "interfaces": [{"name": None, "role": "lan", "ipv4_cidr": "172.16.1.1/24"},
                                               {"name": "f0/0", "role": "transit", "ipv4_cidr": "172.16.12.1/30"}]},
            {"name": "R2", "interfaces": [{"name": "Fa1/0", "role": "transit", "ipv4_cidr": "172.16.12.2/30"},
                                          {"name": None, "role": "lan", "ipv4_cidr": "172.16.2.1/24"}]}],
        "ambiguities": []})
    notes = []
    p = postprocess(raw, INV, notes)
    r1, r2 = p.routers
    assert r1.name == "R1" and r1.router_id == "1.1.1.1"
    assert r1.interfaces[0].name == "FastEthernet1/0"
    assert any("management" in a for a in p.ambiguities)          # f0/0 used for transit -> flagged
    assert [i.name for i in r2.interfaces if i.role == "transit"] == ["FastEthernet1/1"]   # swapped -> remapped
    assert [i.name for i in r2.interfaces if i.role == "lan"] == ["FastEthernet1/0"]
    assert any(i.role == "management" and i.ipv4_cidr is None for i in r2.interfaces)


def test_repair_appended_suffix_and_drop_non_conflicts():
    from app.ai import repair_appended_cidr

    assert repair_appended_cidr("192.168.10.0.1/24") == "192.168.10.1/24"
    assert repair_appended_cidr("172.16.12.0.2/30") == "172.16.12.2/30"
    assert repair_appended_cidr("172.16.12.0.3/30") is None        # broadcast -> not repaired
    assert repair_appended_cidr("10.0.0.1/24") is None
    raw = TopologyPlan.model_validate({
        "routers": [{"name": "R1", "interfaces": [{"name": "Fa1/0", "role": "lan", "ipv4_cidr": "192.168.10.0.1/24"}]}],
        "ambiguities": ["R1 LAN: IP address format '192.168.10.0.1' is invalid",
                        "R1 LAN: diagram label Fa1/0 matches inventory FastEthernet1/0",
                        "R1 LAN: diagram label Fa0/0 differs from inventory FastEthernet1/0"]})
    notes = []
    p = postprocess(raw, INV, notes)
    assert p.routers[0].interfaces[0].ipv4_cidr == "192.168.10.1/24"
    assert p.ambiguities == ["R1 LAN: diagram label Fa0/0 differs from inventory FastEthernet1/0"]
    assert len(notes) == 3


def test_diagram_label_conflict_is_flagged_deterministically():
    raw = TopologyPlan.model_validate({
        "routers": [{"name": "R1", "interfaces": [
            {"name": "FastEthernet1/0", "role": "lan", "ipv4_cidr": "192.168.10.1/24", "diagram_label": "Fa0/0"},
            {"name": "FastEthernet1/1", "role": "transit", "ipv4_cidr": "172.16.12.1/30", "diagram_label": "Fa1/1"}]}],
        "ambiguities": ["R1 LAN: diagram label Fa0/0 differs from inventory FastEthernet1/0"]})
    p = postprocess(raw, INV, [])
    assert len(p.ambiguities) == 1, p.ambiguities
    assert "management" in p.ambiguities[0] and "Fa0/0" in p.ambiguities[0]


def test_rollback_never_touches_management():
    p = plan()
    lines = rollback_commands(p.routers[0], 1, "FastEthernet0/0")
    assert "no router ospf 1" in lines
    assert not any("0/0" in ln for ln in lines)


def test_with_connect_router_opens_one_session(monkeypatch=None):
    """Regression: `with connect_router(...)` must not log in twice (leaked IOS vty lines)."""
    from app import router_ssh

    calls = {"connect": 0, "close": 0}

    class FakeChan:
        closed = False

    def fake_connect(self):
        calls["connect"] += 1
        self.chan, self.hostname, self.privileged = FakeChan(), "R1", True

    def fake_close(self):
        calls["close"] += 1
        self.chan = None

    orig = (router_ssh.RouterSession.connect, router_ssh.RouterSession.close)
    router_ssh.RouterSession.connect, router_ssh.RouterSession.close = fake_connect, fake_close
    try:
        with router_ssh.connect_router("192.0.2.1", expected_hostname="R1") as s:
            assert s.hostname == "R1"
    finally:
        router_ssh.RouterSession.connect, router_ssh.RouterSession.close = orig
    assert calls == {"connect": 1, "close": 1}, calls


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("PASS", name)
            except AssertionError as exc:
                fails += 1
                print("FAIL", name, exc)
    sys.exit(1 if fails else 0)
