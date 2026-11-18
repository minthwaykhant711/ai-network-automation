# AI Network Automation — OSPF (lab exam)

Upload a topology diagram → an image-capable AI extracts devices, links and addressing as JSON →
the administrator reviews/resolves, validates and **approves** → the approved job goes to **Kafka** →
a **Python worker** deploys it with **Paramiko** SSH → OSPF is verified automatically → PC1 pings PC2.

```
Browser  /  (upload · AI · review & resolve · validate · approve · deploy)     /jobs  (monitor)
   │
   ▼
FastAPI backend  app/main.py ── read-only SSH for validate/discover (app/validation.py)
   │  plans + jobs as JSON in data/ (atomic writes); deploy = job PENDING + Kafka message (IDs + hash only)
   ▼
Kafka topic network-config-jobs   (Docker, KRaft, 127.0.0.1:39092 — compose.yaml)
   ▼
worker.py  (consumer, manual commit, single instance via file lock)
   │  POST /api/jobs/{id}/claim  → backend re-checks approval + SHA-256, PENDING→RUNNING exactly once
   │  per router: SSH → hostname == device ID → backup running-config → push approved lines → output
   │  verify: show ip ospf neighbor / show ip route ospf / show ip protocols (+ no static routes)
   │  POST /api/jobs/{id}/progress … SUCCESS | FAILED → commit offset
   ▼
R1 / R2 (c7200, IOS 15.2) — data plane only; the management interface is never configured
```

The AI never talks to routers. The backend only *reads* routers. Every configuration change goes
backend → Kafka → worker → Paramiko, and only for an approved plan whose hash still matches.

## Exam tasks → where they are implemented

| Task | Implementation |
|---|---|
| 1 Web frontend | `app/static/index.html` + `app.js`: upload + preview PNG/JPG, **Generate**, **Review commands**, **Validate**, **Approve**, **Deploy** |
| 2 AI analysis | `app/ai.py`: Groq `qwen/qwen3.8-27b` (vision), JSON mode, `temperature=0`, schema `app/models.py`; ambiguities flagged (null instead of guessing); structured + raw JSON shown |
| 3 IOS/OSPF generation | `app/templates/router_ospf.j2` rendered by `app/ios.py`: interfaces, OSPF process/area, router-id, `passive-interface` on LANs, transit active, one `network` per /24 |
| 4 Validation & approval | `app/validation.py`: addressing, network/broadcast, duplicates, subnet consistency/overlap, transit has a neighbor, PC gateways, OSPF settings, **live**: SSH reachability, hostname, privilege 15, interface exists, never the SSH-carrying interface. Approval needs the checkbox + typed confirmation and re-validates; stores `config_sha256` |
| 5 Kafka producer | `POST /api/plans/{id}/deploy`: topic `network-config-jobs`, message `{job_id, plan_id, device_ids, config_sha256, approved_config_ref, requested_at}` |
| 6 Worker | `worker.py`: consumes, **claims via backend** (approval + hash re-check), reports progress per stage, **duplicate prevention** (claim only from PENDING; replays are refused), manual offset commit |
| 7 Paramiko | `app/router_ssh.py`: credentials from `.env`, strict host keys, auth/timeout/host-key errors mapped to readable messages, IOS error detection, full output collected, running-config backup |
| 8 Verification | `worker.py verify_router`: FULL neighbor on the expected interface, `O` route to every remote network, `ospf 1` + router ID, no `S` routes; retried until converged (≤120 s); ping far PC from router (info) |
| 9 Job monitor | `/jobs`: PENDING/RUNNING/SUCCESS/FAILED, routers, created/started/finished, timeline, deployment output, ✔/✘ checks, raw show outputs, Kafka message |
| 10 E2E | `docs/DEMO.md` runbook; PC1 → `ping 10.2.0.10` |

## Lab assumptions (this PC)

| Item | Value |
|---|---|
| Routers | c7200 IOS 15.2(4)S5, slot1 PA-2FE-TX |
| Management | `FastEthernet0/0` → Switch1 → Cloud1 (GNS3 VM, VMnet1 192.168.27.0/24, DHCP) |
| Data plane | `FastEthernet1/0` = LAN (PC), `FastEthernet1/1` = R1–R2 link |
| Mgmt IPs | R1 192.168.27.131, R2 192.168.27.132 (DHCP — re-check after restarts) |
| App host | Ubuntu VM 192.168.50.136 (VMnet8 NAT; reaches VMnet1 through VMware NAT) |

All of this lives in `.env`, not in code. Another topology/addressing plan only needs a new diagram;
a different router or wiring only needs `.env` inventory changes (`R3_HOST=…` adds a router).

## Setup (Ubuntu VM)

```bash
sudo apt install -y python3-venv python3-pip docker.io docker-compose-v2   # once
cd ~/ai-network-exam
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env    # then fill GROQ_API_KEY, SSH_PASSWORD (and check R*_HOST)
.venv/bin/python lab/check_routers.py --trust   # learn router host keys + SSH check
./run.sh start                                  # Kafka + topic + backend + worker
```

Open `http://<vm-ip>:8000/` (workflow) and `http://<vm-ip>:8000/jobs` (monitor).
`./run.sh status | stop | restart`; logs in `data/logs/`.

Routers need a one-time management bootstrap typed by the operator (`lab/router_bootstrap.txt`):
hostname, local admin user (privilege 15), RSA key, SSH v2, `ip address dhcp` on the management port.
Do **not** `write memory` after automated deployments, so a reload restores the clean state.

## Recovery

* Job FAILED → details in `/jobs` (which router, which stage, IOS output). Fix the plan and redeploy
  (FAILED plans may be redeployed as a new job) or **Revoke** to return the plan to DRAFT.
* Undo the data-plane config: `.venv/bin/python worker.py --rollback latest` (or a plan ID) —
  removes `router ospf`, defaults and shuts the LAN/transit interfaces, never touches management.
* "router refused SSH": IOS has 5 vty lines; sessions always log out with `exit`, but if lines are
  stuck run `show users` on the console (they clear at exec-timeout, default 10 min).
* Host key changed / new DHCP address: update `R*_HOST` in `.env`, run `lab/check_routers.py --trust`.

## Security

* Secrets only in `.env` (chmod 600, git-ignored): `GROQ_API_KEY`, `SSH_USERNAME`, `SSH_PASSWORD`.
* Kafka messages carry IDs and a hash, never configuration text or credentials; the worker fetches the
  approved config from the backend and re-checks the SHA-256 before touching a router.
* Worker callbacks (`claim`, `progress`) require a locally generated token (`data/run/worker_token`).
* Optional `ADMIN_TOKEN` in `.env` makes edit/approve/deploy require that token.
* Strict SSH host-key checking (RejectPolicy). Kafka is bound to 127.0.0.1.
* Generated config is checked to never include the management interface (validator, generator and
  worker each refuse it).

## Tests

```bash
.venv/bin/python -m pytest -q tests     # offline: template output, validation rules, AI post-processing
```

## Notes

* `paramiko==3.5.1` is pinned: Paramiko ≥ 4 dropped `diffie-hellman-group14-sha1` / `ssh-rsa`, the
  only algorithms IOS 15.2 offers (`LegacyCiscoTransport`).
* Groq free tier allows roughly one analysis per minute (HTTP 429 → wait and click Generate again).
