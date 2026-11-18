# Short report — AI-driven OSPF network automation

## Architecture
A FastAPI web application accepts a PNG/JPG topology diagram and sends it to an image-capable LLM
(Groq `qwen/qwen3.8-27b`, JSON mode, temperature 0). The model only *transcribes* the picture into a
typed `TopologyPlan` (routers, interfaces with role and IPv4/prefix, PCs, links, OSPF settings,
ambiguities). Deterministic post-processing then maps roles to the real physical ports from the router
inventory, flags diagram-label conflicts, repairs the unambiguous "subnet label + host suffix" pattern, and
never invents addresses. The administrator reviews and resolves ambiguities in the web editor, checks the
interface mapping against the live routers (read-only `show ip interface brief`), validates, and approves.

Approval stores the exact per-router command list and its SHA-256. Deploy creates a PENDING job and
publishes a Kafka message on `network-config-jobs` containing only IDs, the device list, the hash and a
reference to the approved configuration. The Python worker consumes it, **claims** the job from the
backend (which re-checks approval and hash and moves PENDING→RUNNING exactly once), fetches the approved
configuration, re-verifies the hash, and deploys over SSH with Paramiko (one session per router: backup
running-config, apply, collect output, detect IOS errors). It then verifies OSPF automatically
(`show ip ospf neighbor`, `show ip route ospf`, `show ip protocols`, no static routes) until converged, and
reports every stage to the backend, which the `/jobs` monitor displays.

Safety: the AI never connects to routers; the backend only reads them; the management interface is
refused by the validator, the generator and the worker; credentials and API key live only in `.env`;
host keys are checked strictly; replayed Kafka messages are rejected; a rollback command restores the data
plane without touching management.

## Test results (2026-11-18, this lab)

| Test | Result |
|---|---|
| Offline unit tests (`tests/test_offline.py`) | 12 passed |
| Exam diagram → AI | 0 ambiguities; R1/R2 interfaces, /24 addresses, router IDs, PCs correct (3 runs) |
| Generated R1 config | identical to the expected template output (`interface FastEthernet1/0 … network 10.1.0.0 0.0.0.255 area 0`) |
| Validate (static + live SSH) | PASSED; both routers reachable, hostname match, privilege 15 |
| Approval gate | deploy unapproved → 409; approve without "APPROVE" → 400 |
| Deploy from reset state | job PENDING → RUNNING → **SUCCESS** in ≈65 s; Kafka partition 0, offsets 2/5 |
| OSPF verification | R1: 2.2.2.2 FULL on Fa1/1, `O 10.2.0.0/24`; R2: 1.1.1.1 FULL on Fa1/1, `O 10.0.0.0/24`; router IDs ok; no static routes |
| End-to-end | **PC1 `ping 10.2.0.10`: 5/5 replies, TTL 62** |
| Duplicate execution | replayed Kafka message → worker: "Job is SUCCESS; it will not be executed again" |
| Alternative diagram (192.168.10.0/24, 172.16.12.0/30, 192.168.20.0/24, Fa0/0 label, no PCs) | label conflict flagged → resolved in editor → validated → deployed → SUCCESS, `O 192.168.20.0/24` via /30 |
| Management protection | LAN placed on FastEthernet0/0 → rejected by validator, generator and live check |
| Rollback (`worker.py --rollback`) | Fa1/0, Fa1/1 unassigned + admin down, OSPF removed, Fa0/0 still up |

## Problems found and fixed during testing
* **Leaked IOS vty lines.** `with connect_router(...)` logged in twice (the context manager reconnected an
  already-connected session), so the long-running backend/worker leaked one SSH session per use; after five,
  IOS refused SSH and verification failed. Fixed (`RouterSession.__enter__`), regression test added,
  sessions also log out with `exit`, and the worker uses one session per router per job.
* **AI address concatenation** (`192.168.10.0` + `.1` → `192.168.10.0.1/24`) and a non-conflict reported as
  an ambiguity — prompt tightened; deterministic repair and filtering added with notes shown in the UI.
* **Diagram label conflicts** were reported by the model only sometimes — now the model transcribes the
  label (`diagram_label`) and code compares it with the real port.
