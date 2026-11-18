"""Plan validation: static (addressing, subnets, OSPF, interface mapping) + live read-only SSH checks."""
from __future__ import annotations

import ipaddress
from concurrent.futures import ThreadPoolExecutor

from app.config import RouterInventory, policy_router_id
from app.ifnames import normalize_interface, same_interface
from app.ios import ConfigError, data_interfaces, render_plan
from app.models import TopologyPlan
from app.router_ssh import RouterSSHError, connect_router, parse_ip_interface_brief


def static_checks(plan: TopologyPlan, inventory: dict[str, RouterInventory]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    if plan.ambiguities:
        errors.append(f"{len(plan.ambiguities)} unresolved ambiguit{'y' if len(plan.ambiguities) == 1 else 'ies'} "
                      f"- review and resolve them first")
    if not plan.routers:
        errors.append("No routers in the plan")
        return errors, warnings

    # --- OSPF settings
    if not plan.ospf_process_id or not (1 <= int(plan.ospf_process_id) <= 65535):
        errors.append(f"Invalid OSPF process ID {plan.ospf_process_id!r}")
    try:
        area = str(plan.ospf_area)
        if "." in area:
            ipaddress.IPv4Address(area)
        elif not (0 <= int(area) <= 4294967295):
            raise ValueError
    except (ValueError, TypeError):
        errors.append(f"Invalid OSPF area {plan.ospf_area!r}")

    names = [r.name for r in plan.routers]
    if len(set(names)) != len(names):
        errors.append("Duplicate router names in plan")

    rids = {}
    segments: dict[ipaddress.IPv4Network, list[tuple[str, str, str]]] = {}   # net -> [(router, intf, role)]
    all_ips: dict[ipaddress.IPv4Address, str] = {}

    for router in plan.routers:
        inv = inventory.get(router.name)
        if inv is None:
            errors.append(f"{router.name}: not in the router inventory (.env has no {router.name}_HOST)")
        # router ID
        if not router.router_id:
            errors.append(f"{router.name}: missing OSPF router ID")
        else:
            try:
                ipaddress.IPv4Address(router.router_id)
                if router.router_id in rids:
                    errors.append(f"{router.name}: router ID {router.router_id} duplicates {rids[router.router_id]}")
                rids[router.router_id] = router.name
                pol = policy_router_id(router.name)
                if pol and pol != router.router_id:
                    warnings.append(f"{router.name}: router ID {router.router_id} differs from course policy {pol}")
            except ValueError:
                errors.append(f"{router.name}: invalid router ID '{router.router_id}'")

        # interfaces
        try:
            intfs = data_interfaces(router)
        except ConfigError as exc:
            errors.append(str(exc))
            continue
        if not intfs:
            errors.append(f"{router.name}: no LAN or transit interfaces")
        seen_names = set()
        for i in intfs:
            key = i["name"].lower()
            if key in seen_names:
                errors.append(f"{router.name}: interface {i['name']} used twice")
            seen_names.add(key)
            if inv and inv.management_interface and same_interface(i["name"], inv.management_interface):
                errors.append(f"{router.name}: {i['name']} is the management/SSH interface - "
                              f"it must not carry {i['role']} addressing")
            iface = i["interface"]
            net = iface.network
            if net.prefixlen < 31 and iface.ip in (net.network_address, net.broadcast_address):
                errors.append(f"{router.name} {i['name']}: {iface.ip} is the network/broadcast address of {net}")
            if iface.ip in all_ips:
                errors.append(f"{router.name} {i['name']}: IP {iface.ip} duplicates {all_ips[iface.ip]}")
            all_ips[iface.ip] = f"{router.name} {i['name']}"
            segments.setdefault(net, []).append((router.name, i["name"], i["role"]))
        for intf in router.interfaces:
            if intf.role == "unknown" and intf.ipv4_cidr:
                errors.append(f"{router.name} {intf.name or '?'}: role is 'unknown' - set lan or transit")

    # --- subnet consistency
    nets = list(segments)
    for a_idx, a in enumerate(nets):
        for b in nets[a_idx + 1:]:
            if a.overlaps(b):
                errors.append(f"Overlapping subnets {a} and {b} (inconsistent prefix lengths?)")
    transit_total = 0
    for net, members in segments.items():
        roles = {m[2] for m in members}
        routers_on = {m[0] for m in members}
        if "transit" in roles:
            transit_total += 1
            if len(routers_on) < 2:
                errors.append(f"Transit subnet {net} has only {', '.join(routers_on)} - no OSPF neighbor on it")
            if "lan" in roles:
                errors.append(f"Subnet {net} is LAN on one router and transit on another")
        if roles == {"lan"} and len(routers_on) > 1:
            warnings.append(f"LAN {net} is shared by {', '.join(sorted(routers_on))}; it is passive, so no adjacency "
                            f"forms there")
    if len(plan.routers) > 1 and transit_total == 0:
        errors.append("No transit (router-to-router) link - OSPF adjacency cannot form")

    # --- links (if the AI drew them) must agree with addressing
    router_names = set(names)
    for link in plan.links:
        if link.device_a in router_names and link.device_b in router_names:
            na, nb = _intf_net(plan, link.device_a, link.interface_a), _intf_net(plan, link.device_b, link.interface_b)
            if na and nb and na != nb:
                errors.append(f"Link {link.device_a} {link.interface_a} - {link.device_b} {link.interface_b}: "
                              f"subnets differ ({na} vs {nb})")

    # --- PCs are optional; check only what is given
    lan_nets = {net: m for net, m in segments.items() if any(x[2] == "lan" for x in m)}
    for pc in plan.pcs:
        if not pc.ipv4_cidr:
            continue
        try:
            pci = ipaddress.IPv4Interface(pc.ipv4_cidr)
        except ValueError:
            errors.append(f"{pc.name}: invalid IPv4 '{pc.ipv4_cidr}'")
            continue
        if pci.ip in all_ips:
            errors.append(f"{pc.name}: IP {pci.ip} duplicates {all_ips[pci.ip]}")
        if pci.network not in lan_nets:
            errors.append(f"{pc.name}: {pci.network} is not a router LAN subnet")
            continue
        if pc.gateway:
            try:
                gw = ipaddress.IPv4Address(pc.gateway)
            except ValueError:
                errors.append(f"{pc.name}: invalid gateway '{pc.gateway}'")
                continue
            if gw not in pci.network:
                errors.append(f"{pc.name}: gateway {gw} is outside {pci.network}")
            elif all_ips.get(gw) is None:
                errors.append(f"{pc.name}: gateway {gw} is not a router interface address")
        else:
            warnings.append(f"{pc.name}: no default gateway given")

    _, render_errors = render_plan(plan, inventory)
    errors.extend(e for e in render_errors if e not in errors)
    return errors, warnings


def _intf_net(plan: TopologyPlan, device: str, intf_name: str | None):
    for r in plan.routers:
        if r.name != device:
            continue
        for i in r.interfaces:
            if intf_name and same_interface(i.name, intf_name) and i.ipv4_cidr:
                try:
                    return ipaddress.IPv4Interface(i.ipv4_cidr).network
                except ValueError:
                    return None
    return None


# ---------------------------------------------------------------- live (read-only)
def _probe_router(name: str, inv: RouterInventory) -> dict:
    result = {"router": name, "host": inv.host, "reachable": False, "hostname": None, "privilege_15": False,
              "interfaces": [], "ssh_interface": None, "error": None}
    try:
        with connect_router(inv.host) as s:
            result.update(reachable=True, hostname=s.hostname, privilege_15=s.privileged)
            rows = parse_ip_interface_brief(s.send("show ip interface brief"))
            for row in rows:
                row["carries_ssh"] = row["ip"] == inv.host
            result["interfaces"] = rows
            result["ssh_interface"] = next((r["name"] for r in rows if r["carries_ssh"]), None)
    except RouterSSHError as exc:
        result["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 - report anything to the operator
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def discover_interfaces(router_names: list[str], inventory: dict[str, RouterInventory]) -> dict:
    jobs = {n: inventory[n] for n in router_names if n in inventory}
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
        results = dict(zip(jobs, pool.map(lambda kv: _probe_router(*kv), jobs.items())))
    for n in router_names:
        if n not in inventory:
            results[n] = {"router": n, "reachable": False, "error": f"no {n}_HOST in .env", "interfaces": []}
    return results


def live_checks(plan: TopologyPlan, inventory: dict[str, RouterInventory]) -> tuple[list[str], list[str], dict]:
    errors, warnings = [], []
    probes = discover_interfaces([r.name for r in plan.routers], inventory)
    for router in plan.routers:
        p = probes.get(router.name, {})
        if not p.get("reachable"):
            errors.append(f"{router.name}: management SSH unreachable - {p.get('error')}")
            continue
        if (p.get("hostname") or "").lower() != router.name.lower():
            errors.append(f"{router.name}: device at {p['host']} reports hostname '{p.get('hostname')}'")
        if not p.get("privilege_15"):
            errors.append(f"{router.name}: SSH user lacks privilege 15")
        names = {r["name"].lower(): r for r in p.get("interfaces", [])}
        inv = inventory[router.name]
        if inv.management_interface and p.get("ssh_interface") and \
                not same_interface(inv.management_interface, p["ssh_interface"]):
            warnings.append(f"{router.name}: SSH arrives on {p['ssh_interface']} but .env says management is "
                            f"{inv.management_interface}")
        for intf in router.interfaces:
            if intf.role not in ("lan", "transit"):
                continue
            n = normalize_interface(intf.name) or ""
            row = names.get(n.lower())
            if row is None:
                errors.append(f"{router.name}: interface {n} does not exist on the router "
                              f"(has: {', '.join(r['name'] for r in p.get('interfaces', []))})")
            elif row.get("carries_ssh"):
                errors.append(f"{router.name}: {n} carries the management SSH session ({p['host']}) - refusing")
    return errors, warnings, probes


def validate_plan(plan: TopologyPlan, inventory: dict[str, RouterInventory], live: bool = True) -> dict:
    errors, warnings = static_checks(plan, inventory)
    probes = {}
    if live and not any("not in the router inventory" in e for e in errors):
        lerr, lwarn, probes = live_checks(plan, inventory)
        errors += lerr
        warnings += lwarn
    return {"passed": not errors, "errors": errors, "warnings": warnings, "live": probes}
