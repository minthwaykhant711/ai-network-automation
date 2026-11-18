"""Turns a validated topology plan into per-router IOS commands (Jinja2 template)."""
from __future__ import annotations

import hashlib
import ipaddress
import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.ifnames import normalize_interface, same_interface
from app.models import Router, TopologyPlan

_ENV = Environment(
    loader=FileSystemLoader(Path(__file__).resolve().parent / "templates"),
    undefined=StrictUndefined,
    trim_blocks=False,
    keep_trailing_newline=False,
)
TEMPLATE = "router_ospf.j2"


class ConfigError(ValueError):
    pass


def data_interfaces(router: Router) -> list[dict]:
    """LAN interfaces first, then transit, each in plan order; parsed + checked."""
    out = []
    for role in ("lan", "transit"):
        for intf in router.interfaces:
            if intf.role != role:
                continue
            name = normalize_interface(intf.name)
            if not name:
                raise ConfigError(f"{router.name}: a {role} interface has no interface name")
            if not intf.ipv4_cidr:
                raise ConfigError(f"{router.name} {name}: missing IPv4 address/prefix")
            try:
                iface = ipaddress.IPv4Interface(intf.ipv4_cidr)
            except ValueError as exc:
                raise ConfigError(f"{router.name} {name}: invalid IPv4 '{intf.ipv4_cidr}' ({exc})") from exc
            out.append({"name": name, "role": role, "interface": iface})
    return out


def render_router(router: Router, plan: TopologyPlan, management_interface: str | None) -> list[str]:
    intfs = data_interfaces(router)
    if not intfs:
        raise ConfigError(f"{router.name}: no LAN or transit interfaces to configure")
    for i in intfs:
        if management_interface and same_interface(i["name"], management_interface):
            raise ConfigError(
                f"{router.name}: {i['name']} is the management (SSH) interface and must never be reconfigured")
    if not router.router_id:
        raise ConfigError(f"{router.name}: missing OSPF router ID")
    try:
        ipaddress.IPv4Address(router.router_id)
    except ValueError as exc:
        raise ConfigError(f"{router.name}: invalid router ID '{router.router_id}'") from exc

    networks, seen = [], set()
    for i in intfs:
        net = i["interface"].network
        if net in seen:
            continue
        seen.add(net)
        networks.append({"address": str(net.network_address), "wildcard": str(net.hostmask)})

    text = _ENV.get_template(TEMPLATE).render(
        process_id=plan.ospf_process_id,
        area=plan.ospf_area,
        router_id=router.router_id,
        interfaces=[{"name": i["name"], "address": str(i["interface"].ip),
                     "netmask": str(i["interface"].netmask)} for i in intfs],
        passive=[i["name"] for i in intfs if i["role"] == "lan"],
        active=[i["name"] for i in intfs if i["role"] == "transit"],
        networks=networks,
    )
    return [line.rstrip() for line in text.splitlines() if line.strip()]


def render_plan(plan: TopologyPlan, inventory: dict) -> tuple[dict[str, list[str]], list[str]]:
    """Returns (commands_by_router, errors)."""
    commands, errors = {}, []
    for router in plan.routers:
        inv = inventory.get(router.name)
        try:
            commands[router.name] = render_router(router, plan, inv.management_interface if inv else None)
        except ConfigError as exc:
            errors.append(str(exc))
    return commands, errors


def config_digest(commands_by_router: dict[str, list[str]]) -> str:
    payload = json.dumps(commands_by_router, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def rollback_commands(router: Router, process_id: int, management_interface: str | None) -> list[str]:
    """Recovery: remove OSPF and reset the data-plane interfaces. Management is never touched."""
    lines = ["configure terminal", f"no router ospf {process_id}"]
    for intf in router.interfaces:
        name = normalize_interface(intf.name)
        if intf.role not in ("lan", "transit") or not name:
            continue
        if management_interface and same_interface(name, management_interface):
            continue
        lines += [f"default interface {name}", f"interface {name}", "shutdown", "exit"]
    lines.append("end")
    return lines
