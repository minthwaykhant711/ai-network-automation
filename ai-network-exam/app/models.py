"""Structured topology plan produced by the AI and edited by the administrator."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Role = Literal["lan", "transit", "management", "unknown"]


def _clean_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s and s.lower() not in {"null", "none", "n/a", "unknown", "?"} else None


class Interface(BaseModel):
    name: str | None = None
    role: Role = "unknown"
    ipv4_cidr: str | None = None
    diagram_label: str | None = None      # port label printed in the image, verbatim (null if none)

    @field_validator("name", "ipv4_cidr", "diagram_label", mode="before")
    @classmethod
    def _strip(cls, v):
        return _clean_str(v)

    @field_validator("role", mode="before")
    @classmethod
    def _role(cls, v):
        v = (str(v or "unknown")).strip().lower()
        return v if v in {"lan", "transit", "management", "unknown"} else "unknown"


class Router(BaseModel):
    name: str
    router_id: str | None = None
    interfaces: list[Interface] = Field(default_factory=list)

    @field_validator("router_id", mode="before")
    @classmethod
    def _rid(cls, v):
        return _clean_str(v)


class PC(BaseModel):
    name: str
    ipv4_cidr: str | None = None
    gateway: str | None = None
    port: str = "e0"

    @field_validator("ipv4_cidr", "gateway", mode="before")
    @classmethod
    def _strip(cls, v):
        return _clean_str(v)


class Link(BaseModel):
    device_a: str
    interface_a: str | None = None
    device_b: str
    interface_b: str | None = None

    @field_validator("interface_a", "interface_b", mode="before")
    @classmethod
    def _strip(cls, v):
        return _clean_str(v)


class TopologyPlan(BaseModel):
    routers: list[Router] = Field(default_factory=list)
    pcs: list[PC] = Field(default_factory=list)
    other_devices: list[str] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    ospf_process_id: int | None = 1
    ospf_area: str | None = "0"
    ambiguities: list[str] = Field(default_factory=list)

    @field_validator("ospf_area", mode="before")
    @classmethod
    def _area(cls, v):
        return None if v is None else str(v).strip()

    @field_validator("other_devices", mode="before")
    @classmethod
    def _others(cls, v):
        out = []
        for item in v or []:
            if isinstance(item, dict):
                item = item.get("name") or item.get("type") or str(item)
            out.append(str(item))
        return out

    @field_validator("ambiguities", mode="before")
    @classmethod
    def _amb(cls, v):
        out = []
        for item in v or []:
            if isinstance(item, dict):
                item = item.get("description") or item.get("issue") or item.get("message") or str(item)
            s = str(item).strip()
            if s:
                out.append(s)
        return out
