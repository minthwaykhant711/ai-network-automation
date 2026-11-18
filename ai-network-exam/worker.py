#!/usr/bin/env python3
"""Kafka worker: consumes network-config-jobs, re-checks approval with the backend, deploys with
Paramiko, verifies OSPF, and reports progress. Single instance (file lock); offsets committed manually
only after a job reaches a terminal state, and the backend's claim step makes execution exactly-once.

    python worker.py                     # run the consumer
    python worker.py --rollback latest   # recovery: remove OSPF + reset data interfaces of a plan
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import re
import signal
import socket
import sys
import time

import requests

from app import config
from app.ifnames import same_interface
from app.ios import config_digest, rollback_commands
from app.models import TopologyPlan
from app.router_ssh import RouterSSHError, connect_router

log = logging.getLogger("worker")
WORKER_ID = f"{socket.gethostname()}-{hashlib.sha1(str(time.time()).encode()).hexdigest()[:6]}"
RUNNING = True


# ------------------------------------------------------------------ backend API
def api(method: str, path: str, **kw) -> requests.Response:
    headers = kw.pop("headers", {})
    headers["X-Worker-Token"] = config.worker_token()
    return requests.request(method, config.BACKEND_URL + path, headers=headers, timeout=30, **kw)


def report(job_id: str, stage: str, status: str = "RUNNING", **fields) -> None:
    body = {"status": status, "stage": stage, **fields}
    for attempt in range(5):
        try:
            r = api("POST", f"/api/jobs/{job_id}/progress", json=body)
            if r.status_code == 200:
                return
            log.warning("progress %s rejected: %s %s", stage, r.status_code, r.text[:200])
            return
        except requests.RequestException as exc:
            log.warning("progress %s failed (%s), retrying", stage, exc)
            time.sleep(2 * (attempt + 1))


# ------------------------------------------------------------------ verification
NEIGHBOR_RE = re.compile(r"^(\d+\.\d+\.\d+\.\d+)\s+\d+\s+(\S+)\s+\S+\s+(\d+\.\d+\.\d+\.\d+)\s+(\S+)", re.M)


def expected_for(router_name: str, topo: TopologyPlan) -> dict:
    import ipaddress

    nets = {}           # router -> {net: role}
    rids = {r.name: r.router_id for r in topo.routers}
    for r in topo.routers:
        nets[r.name] = {}
        for i in r.interfaces:
            if i.role in ("lan", "transit") and i.ipv4_cidr:
                nets[r.name][ipaddress.IPv4Interface(i.ipv4_cidr).network] = (i.role, i.name)
    mine = nets[router_name]
    remote = sorted({str(n) for other, ns in nets.items() if other != router_name for n in ns if n not in mine})
    neighbors = {}
    for net, (role, ifname) in mine.items():
        if role != "transit":
            continue
        for other, ns in nets.items():
            if other != router_name and net in ns and ns[net][0] == "transit":
                neighbors[rids[other]] = ifname
    remote_pcs = []
    for pc in topo.pcs:
        if pc.ipv4_cidr:
            pci = ipaddress.IPv4Interface(pc.ipv4_cidr)
            if pci.network not in mine:
                remote_pcs.append(str(pci.ip))
    rid = rids[router_name]
    return {"remote_networks": remote, "neighbors": neighbors, "router_id": rid, "remote_pcs": remote_pcs}


def route_present(output: str, network: str) -> bool:
    addr = network.split("/")[0]
    for line in output.splitlines():
        if not re.match(r"^O(\s|\*|$)", line):
            continue
        tokens = re.split(r"[\s,]+", line)
        if any(t == network or t == addr or t.startswith(addr + "/") for t in tokens):
            return True
    return False


def verify_router(session, name: str, topo: TopologyPlan) -> dict:
    exp = expected_for(name, topo)
    pid = topo.ospf_process_id
    nbr = session.send("show ip ospf neighbor")
    rte = session.send("show ip route ospf")
    proto = session.send("show ip protocols")
    static = session.send("show ip route static")
    full = {m.group(1): m.group(4) for m in NEIGHBOR_RE.finditer(nbr) if m.group(2).upper().startswith("FULL")}
    neighbor_problems = []
    for rid, ifname in exp["neighbors"].items():
        if rid not in full:
            neighbor_problems.append(f"neighbor {rid} not FULL")
        elif not same_interface(full[rid], ifname):
            neighbor_problems.append(f"neighbor {rid} FULL on {full[rid]}, expected {ifname}")
    missing = [n for n in exp["remote_networks"] if not route_present(rte, n)]
    process_ok = f'"ospf {pid}"' in proto
    rid_ok = bool(re.search(rf"Router ID {re.escape(exp['router_id'] or '')}\b", proto))
    statics = [ln.strip() for ln in static.splitlines() if re.match(r"^S\s", ln)]
    problems = neighbor_problems + [f"missing OSPF route to {n}" for n in missing]
    if not process_ok:
        problems.append(f"OSPF process {pid} not running")
    if not rid_ok:
        problems.append(f"router ID is not {exp['router_id']}")
    if statics:
        problems.append(f"static routes present: {statics}")
    return {
        "passed": not problems, "problems": problems,
        "neighbor_full": not neighbor_problems, "neighbors_expected": exp["neighbors"], "neighbors_full": full,
        "remote_networks_expected": exp["remote_networks"], "remote_networks_missing": missing,
        "ospf_process_ok": process_ok, "router_id_ok": rid_ok, "static_routes_found": statics,
        "remote_pcs": exp["remote_pcs"],
        "show_ip_ospf_neighbor": nbr, "show_ip_route_ospf": rte, "show_ip_protocols": proto,
    }


def ping_from_router(session, target: str) -> dict:
    out = session.send(f"ping {target} repeat 5 timeout 1", timeout=30)
    m = re.search(r"Success rate is (\d+) percent \((\d+)/(\d+)\)", out)
    return {"target": target, "success_percent": int(m.group(1)) if m else 0, "output": out}


# ------------------------------------------------------------------ job execution
def execute(msg: dict) -> None:
    job_id, plan_id = msg["job_id"], msg["plan_id"]
    log.info("job %s: claiming", job_id)
    r = api("POST", f"/api/jobs/{job_id}/claim", json={"config_sha256": msg["config_sha256"], "worker_id": WORKER_ID})
    if r.status_code == 409:
        log.warning("job %s: %s", job_id, r.json().get("detail"))
        return
    r.raise_for_status()

    cfg = api("GET", msg["approved_config_ref"])
    if cfg.status_code != 200:
        report(job_id, "fetch-config", "FAILED", error=f"cannot fetch approved config: {cfg.text[:200]}")
        return
    cfg = cfg.json()
    commands: dict = cfg["commands"]
    if config_digest(commands) != msg["config_sha256"] or cfg["config_sha256"] != msg["config_sha256"]:
        report(job_id, "integrity", "FAILED", error="approved configuration hash mismatch - not deploying")
        return
    if sorted(commands) != sorted(msg["device_ids"]):
        report(job_id, "integrity", "FAILED", error="device list does not match approved configuration")
        return
    topo = TopologyPlan.model_validate(cfg["topology"])
    inventory = config.load_inventory()
    report(job_id, "integrity", message="Approved configuration fetched; SHA-256 verified")

    # One SSH session per router for deploy + verify + ping (IOS has only 5 vty lines).
    sessions: dict = {}

    def session(device: str):
        s = sessions.get(device)
        if s is not None and s.chan is not None and not s.chan.closed:
            return s
        if s is not None:
            s.close()
        s = connect_router(inventory[device].host, expected_hostname=device)
        sessions[device] = s
        return s

    try:
        _deploy_and_verify(job_id, msg, cfg, commands, topo, inventory, session, sessions)
    finally:
        for s in sessions.values():
            s.close()


def _deploy_and_verify(job_id, msg, cfg, commands, topo, inventory, session, sessions) -> None:
    # ---- deploy
    for device in msg["device_ids"]:
        inv = inventory.get(device)
        if inv is None:
            report(job_id, "deploy", "FAILED", device=device, error=f"{device}: no {device}_HOST in worker .env")
            return
        lines = commands[device]
        mgmt = cfg["management_interfaces"].get(device) or inv.management_interface
        if mgmt and any(re.fullmatch(rf"interface\s+{re.escape(mgmt)}", ln.strip(), re.I) for ln in lines):
            report(job_id, "deploy", "FAILED", device=device,
                   error=f"{device}: configuration touches management interface {mgmt} - refusing")
            return
        report(job_id, f"deploy {device}", device=device, message=f"Connecting to {device} ({inv.host})")
        try:
            s = session(device)
            backup = s.send("show running-config", timeout=60)
            transcript, errors = s.run_config(lines)
            after = s.send("show ip interface brief")
        except RouterSSHError as exc:
            report(job_id, f"deploy {device}", "FAILED", device=device, error=str(exc),
                   router_results={device: {"host": inv.host, "error": str(exc)}})
            return
        result = {"host": inv.host, "commands": lines, "backup_running_config": backup,
                  "configuration_output": transcript, "interfaces_after": after, "ios_errors": errors}
        if errors:
            report(job_id, f"deploy {device}", "FAILED", device=device, router_results={device: result},
                   error=f"{device}: IOS rejected commands: {'; '.join(errors)}")
            return
        report(job_id, f"deploy {device}", device=device, router_results={device: result},
               message=f"{device}: {len(lines)} lines applied without IOS errors")

    # ---- verify (OSPF needs ~40 s to reach FULL after the process restarts)
    report(job_id, "verify", message=f"Waiting for OSPF convergence (up to {config.OSPF_VERIFY_TIMEOUT:.0f} s)")
    deadline = time.monotonic() + config.OSPF_VERIFY_TIMEOUT
    results: dict = {}
    while True:
        results = {}
        for device in msg["device_ids"]:
            try:
                results[device] = verify_router(session(device), device, topo)
            except RouterSSHError as exc:
                broken = sessions.pop(device, None)
                if broken is not None:
                    broken.close()
                results[device] = {"passed": False, "problems": [str(exc)]}
        if all(v["passed"] for v in results.values()) or time.monotonic() > deadline:
            break
        pending = "; ".join(f"{d}: {', '.join(v['problems'])}" for d, v in results.items() if not v["passed"])
        report(job_id, "verify", message=f"Not converged yet - {pending}")
        time.sleep(10)

    # informational end-to-end check: ping far-side PCs from each router (PCs are configured by hand on VPCS)
    for device, v in results.items():
        if v.get("passed") and v.get("remote_pcs"):
            try:
                s = session(device)
                v["ping_remote_pcs"] = [ping_from_router(s, ip) for ip in v["remote_pcs"]]
            except RouterSSHError as exc:
                v["ping_remote_pcs"] = [{"error": str(exc)}]

    if all(v["passed"] for v in results.values()):
        report(job_id, "done", "SUCCESS", verification=results,
               message="OSPF FULL and all remote networks learned via OSPF on every router")
    else:
        problems = "; ".join(f"{d}: {', '.join(v['problems'])}" for d, v in results.items() if not v["passed"])
        report(job_id, "verify", "FAILED", verification=results, error=f"Verification failed - {problems}")


def run_consumer() -> None:
    from confluent_kafka import Consumer, KafkaError

    lock = open(config.RUN_DIR / "worker.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("Another worker instance is already running")

    consumer = Consumer({"bootstrap.servers": config.KAFKA_BOOTSTRAP_SERVERS, "group.id": config.KAFKA_GROUP_ID,
                         "enable.auto.commit": False, "auto.offset.reset": "earliest",
                         "max.poll.interval.ms": 900000})
    consumer.subscribe([config.KAFKA_TOPIC])
    log.info("worker %s consuming %s from %s", WORKER_ID, config.KAFKA_TOPIC, config.KAFKA_BOOTSTRAP_SERVERS)
    try:
        while RUNNING:
            m = consumer.poll(1.0)
            if m is None:
                continue
            if m.error():
                if m.error().code() != KafkaError._PARTITION_EOF:
                    log.error("kafka error: %s", m.error())
                continue
            try:
                msg = json.loads(m.value())
                for key in ("job_id", "plan_id", "device_ids", "config_sha256", "approved_config_ref"):
                    if key not in msg:
                        raise ValueError(f"missing {key}")
            except (ValueError, json.JSONDecodeError) as exc:
                log.error("discarding malformed message at offset %s: %s", m.offset(), exc)
                consumer.commit(message=m, asynchronous=False)
                continue
            log.info("received job %s (partition %s offset %s)", msg["job_id"], m.partition(), m.offset())
            while RUNNING:
                try:
                    execute(msg)
                    break
                except requests.RequestException as exc:     # backend down: keep the message, retry
                    log.error("backend unavailable (%s); retrying in 5 s", exc)
                    time.sleep(5)
                except Exception as exc:  # noqa: BLE001 - never leave a job RUNNING silently
                    log.exception("job %s crashed", msg["job_id"])
                    report(msg["job_id"], "worker", "FAILED", error=f"worker error: {type(exc).__name__}: {exc}")
                    break
            consumer.commit(message=m, asynchronous=False)
    finally:
        consumer.close()


# ------------------------------------------------------------------ recovery
def rollback(plan_ref: str) -> int:
    if plan_ref == "latest":
        plans = [p for p in api("GET", "/api/plans").json() if p.get("deployment_status") != "NOT_DEPLOYED"]
        if not plans:
            print("No deployed plan found")
            return 1
        plan_ref = plans[0]["id"]
    plan = api("GET", f"/api/plans/{plan_ref}").json()
    topo = TopologyPlan.model_validate(plan["topology"])
    inventory = config.load_inventory()
    print(f"Rolling back plan {plan_ref} (management interfaces are never touched)")
    rc = 0
    for router in topo.routers:
        inv = inventory.get(router.name)
        if inv is None:
            print(f"  {router.name}: not in inventory, skipped")
            rc = 1
            continue
        lines = rollback_commands(router, topo.ospf_process_id, inv.management_interface)
        try:
            with connect_router(inv.host, expected_hostname=router.name) as s:
                out, errors = s.run_config(lines)
                brief = s.send("show ip interface brief")
            print(f"--- {router.name} ({inv.host})\n{out}\n{brief}")
            if errors:
                print(f"  IOS errors: {errors}")
                rc = 1
        except RouterSSHError as exc:
            print(f"  {router.name}: {exc}")
            rc = 1
    return rc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rollback", metavar="PLAN_ID|latest")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("paramiko").setLevel(logging.WARNING)
    if args.rollback:
        return rollback(args.rollback)

    def _stop(*_):
        global RUNNING
        RUNNING = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    run_consumer()
    return 0


if __name__ == "__main__":
    sys.exit(main())
