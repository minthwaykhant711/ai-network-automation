#!/usr/bin/env python3
"""Pre-demo check: management reachability + SSH login + privilege 15 for every router in .env.

    .venv/bin/python lab/check_routers.py           # check
    .venv/bin/python lab/check_routers.py --trust   # (re)learn ssh-rsa host keys first (after DHCP IP changes)
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402
from app.router_ssh import (RouterSSHError, connect_router, fetch_and_trust_host_key,  # noqa: E402
                            parse_ip_interface_brief)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trust", action="store_true", help="fetch and store the routers' host keys")
    args = ap.parse_args()
    rc = 0
    for name, inv in config.load_inventory().items():
        print(f"== {name} {inv.host}")
        if args.trust:
            try:
                print(f"   host key trusted: {fetch_and_trust_host_key(inv.host)}")
            except Exception as exc:  # noqa: BLE001
                print(f"   cannot fetch host key: {exc}")
                rc = 1
                continue
        try:
            with connect_router(inv.host, expected_hostname=name) as s:
                rows = parse_ip_interface_brief(s.send("show ip interface brief"))
                print(f"   SSH OK, hostname {s.hostname}, privilege 15: {s.privileged}")
                for r in rows:
                    mark = "  <- SSH" if r["ip"] == inv.host else ""
                    print(f"   {r['name']:<22}{r['ip']:<16}{r['status']}/{r['protocol']}{mark}")
        except RouterSSHError as exc:
            print(f"   FAILED: {exc}")
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
