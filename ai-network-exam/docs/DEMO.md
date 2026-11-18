# Demo runbook (power-on → ping)

## 1. Power on
1. VMware: start **GNS3 VM** and **Ubuntu 64-bit**.
2. GNS3: open project **AI_Network_Automation_Labest**, start all nodes. Wait ~1 min for the routers
   to boot and get DHCP on `FastEthernet0/0`.
3. On each router console (read-only): `show ip interface brief` → note the Fa0/0 address.
   If it differs from `R1_HOST` / `R2_HOST` in `~/ai-network-exam/.env`, update `.env`.

## 2. Start the system (Ubuntu VM)
```bash
cd ~/ai-network-exam
.venv/bin/python lab/check_routers.py          # add --trust if an address changed
./run.sh start
```
Browser on Windows: `http://192.168.50.136:8000/` and `http://192.168.50.136:8000/jobs`.

## 3. Instructor reset
Routers' data plane is reset (or run `.venv/bin/python worker.py --rollback latest`).
Check: `show ip interface brief` → Fa1/0, Fa1/1 unassigned; `show ip ospf neighbor` empty.

## 4. Workflow (screenshots marked 📷)
1. Choose the diagram → preview → **Generate**. 📷 AI analysis (summary + Structured JSON).
2. Review & resolve: tick + note any ambiguity, **Discover router interfaces** (✔ rows), **Save changes**.
3. **Review commands** → **Validate** (PASSED, both routers reachable). 📷 commands + validation.
4. Tick the confirmation → **Approve** (SHA-256 shown).
5. **Deploy** → confirm. 📷 Kafka job (job ID, partition/offset) + `/jobs` RUNNING.
6. Wait for **SUCCESS** (~1 min). 📷 `/jobs` details: deployment output, ✔ checks, show outputs.

Kafka evidence (📷):
```bash
docker compose exec kafka /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server kafka:9092 \
  --topic network-config-jobs --from-beginning --timeout-ms 5000
```

## 5. Verify on the routers (read-only) 📷
```
R1# show ip ospf neighbor        → 2.2.2.2 FULL on FastEthernet1/1
R1# show ip route ospf           → O 10.2.0.0/24 via 10.1.0.2
R2# show ip route ospf           → O 10.0.0.0/24 via 10.1.0.1
```

## 6. End-to-end 📷
```
PC1> ping 10.2.0.10
```

## Recovery
* FAILED job → read the error in `/jobs`; fix, then Deploy again (new job) or Revoke.
* `.venv/bin/python worker.py --rollback latest` resets the data plane (management untouched).
* Duplicate check: replaying a finished job's Kafka message → worker logs
  "Job is SUCCESS; it will not be executed again".
