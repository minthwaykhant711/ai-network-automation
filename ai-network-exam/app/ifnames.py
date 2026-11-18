"""Cisco interface-name normalisation (f0/0 -> FastEthernet0/0, Gi0/1 -> GigabitEthernet0/1)."""
from __future__ import annotations

import re

_PREFIXES = [
    ("tengigabitethernet", "TenGigabitEthernet"),
    ("te", "TenGigabitEthernet"),
    ("gigabitethernet", "GigabitEthernet"),
    ("gig", "GigabitEthernet"),
    ("gi", "GigabitEthernet"),
    ("g", "GigabitEthernet"),
    ("fastethernet", "FastEthernet"),
    ("fas", "FastEthernet"),
    ("fa", "FastEthernet"),
    ("f", "FastEthernet"),
    ("ethernet", "Ethernet"),
    ("eth", "Ethernet"),
    ("et", "Ethernet"),
    ("e", "Ethernet"),
    ("serial", "Serial"),
    ("se", "Serial"),
    ("s", "Serial"),
    ("loopback", "Loopback"),
    ("lo", "Loopback"),
]

_RE = re.compile(r"^\s*([A-Za-z-]+)\s*([0-9][0-9/.:]*)\s*$")


def normalize_interface(name: str | None) -> str | None:
    if name is None:
        return None
    text = str(name).strip()
    if not text:
        return None
    m = _RE.match(text)
    if not m:
        return text
    word, numbers = m.group(1).lower().replace("-", ""), m.group(2)
    for prefix, full in _PREFIXES:
        if word == prefix:
            return f"{full}{numbers}"
    return text


def short_interface(name: str | None) -> str:
    """FastEthernet1/0 -> Fa1/0 (display only)."""
    n = normalize_interface(name) or ""
    for full, short in (("TenGigabitEthernet", "Te"), ("GigabitEthernet", "Gi"),
                        ("FastEthernet", "Fa"), ("Ethernet", "Et"), ("Serial", "Se"), ("Loopback", "Lo")):
        if n.startswith(full):
            return short + n[len(full):]
    return n


def same_interface(a: str | None, b: str | None) -> bool:
    na, nb = normalize_interface(a), normalize_interface(b)
    return bool(na and nb and na.lower() == nb.lower())


def normalize_pc_port(name: str | None) -> str:
    """VPCS has exactly one port; the diagram may call it e0, eth0, Ethernet0, nic ... -> 'e0'."""
    return "e0"
