"""Runtime configuration loaded from .env (never committed).

Router *inventory* (management host + physical interface names per role) comes from
the environment, so nothing here is tied to one topology: add R3_HOST=... and R3 is
known to the system. Addresses always come from the diagram, never from here.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env", override=False)

DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
PLANS_DIR = DATA_DIR / "plans"
JOBS_DIR = DATA_DIR / "jobs"
UPLOADS_DIR = DATA_DIR / "uploads"
LOGS_DIR = DATA_DIR / "logs"
RUN_DIR = DATA_DIR / "run"
for _d in (PLANS_DIR, JOBS_DIR, UPLOADS_DIR, LOGS_DIR, RUN_DIR):
    _d.mkdir(parents=True, exist_ok=True)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:39092")
KAFKA_TOPIC = os.getenv("KAFKA_TOPIC", "network-config-jobs")
KAFKA_GROUP_ID = os.getenv("KAFKA_GROUP_ID", "network-config-worker")

BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")

SSH_USERNAME = os.getenv("SSH_USERNAME", "")
SSH_PASSWORD = os.getenv("SSH_PASSWORD", "")
SSH_PORT = int(os.getenv("SSH_PORT", "22"))
SSH_TIMEOUT = float(os.getenv("SSH_TIMEOUT", "15"))
SSH_KNOWN_HOSTS = Path(os.path.expanduser(os.getenv("SSH_KNOWN_HOSTS", "~/.ssh/known_hosts")))

OSPF_PROCESS_ID = int(os.getenv("OSPF_PROCESS_ID", "1"))
OSPF_AREA = os.getenv("OSPF_AREA", "0")
OSPF_VERIFY_TIMEOUT = float(os.getenv("OSPF_VERIFY_TIMEOUT", "120"))

WORKER_TOKEN_FILE = RUN_DIR / "worker_token"


def worker_token() -> str:
    """Shared secret between backend and worker for claim/progress calls (generated locally)."""
    if not WORKER_TOKEN_FILE.exists():
        import secrets

        WORKER_TOKEN_FILE.write_text(secrets.token_urlsafe(32))
        WORKER_TOKEN_FILE.chmod(0o600)
    return WORKER_TOKEN_FILE.read_text().strip()


@dataclass
class RouterInventory:
    name: str
    host: str
    management_interface: str | None = None
    lan_interfaces: list[str] = field(default_factory=list)
    transit_interfaces: list[str] = field(default_factory=list)
    router_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "host": self.host,
            "management_interface": self.management_interface,
            "lan_interfaces": self.lan_interfaces,
            "transit_interfaces": self.transit_interfaces,
            "router_id": self.router_id,
        }


def _split(value: str | None) -> list[str]:
    from app.ifnames import normalize_interface

    return [normalize_interface(v.strip()) for v in (value or "").split(",") if v.strip()]


def policy_router_id(name: str) -> str | None:
    """Course policy: R<n> uses router ID n.n.n.n (overridable with <NAME>_ROUTER_ID)."""
    override = os.getenv(f"{name.upper()}_ROUTER_ID")
    if override:
        return override.strip()
    m = re.fullmatch(r"R(\d{1,3})", name.upper())
    if m and 0 < int(m.group(1)) < 256:
        n = m.group(1)
        return f"{n}.{n}.{n}.{n}"
    return None


def load_inventory() -> dict[str, RouterInventory]:
    from app.ifnames import normalize_interface

    inv: dict[str, RouterInventory] = {}
    for key, value in sorted(os.environ.items()):
        m = re.fullmatch(r"([A-Z][A-Z0-9]*)_HOST", key)
        if not m or not value.strip():
            continue
        name = m.group(1)
        if name in {"SSH", "KAFKA", "BACKEND", "GROQ", "OSPF"}:
            continue
        mgmt = os.getenv(f"{name}_MANAGEMENT_INTERFACE")
        inv[name] = RouterInventory(
            name=name,
            host=value.strip(),
            management_interface=normalize_interface(mgmt) if mgmt else None,
            lan_interfaces=_split(os.getenv(f"{name}_LAN_INTERFACE")),
            transit_interfaces=_split(os.getenv(f"{name}_TRANSIT_INTERFACE")),
            router_id=policy_router_id(name),
        )
    return inv
