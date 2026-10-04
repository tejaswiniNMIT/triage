"""
Connector contracts. Everything the agent talks to sits behind one of three
interfaces, so swapping Zoho -> Microsoft -> Atlassian is a config change, never
a code change in the pipeline.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Attachment:
    filename: str
    content: bytes
    content_type: str = "application/octet-stream"

    @property
    def is_tabular(self) -> bool:
        return self.filename.lower().endswith((".csv", ".xlsx", ".xls", ".xlsm", ".txt"))


@dataclass
class InboundMessage:
    message_id: str
    sender: str
    subject: str
    received_at: str
    attachments: list[Attachment] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class TicketRef:
    system: str
    ticket_id: str
    url: str = ""
    status: str = "created"
    error: str | None = None


class SourceConnector(ABC):
    """Where the watchlist files come FROM."""
    name: str = "source"

    @abstractmethod
    def poll(self, since: str | None = None) -> list[InboundMessage]: ...

    @abstractmethod
    def mark_processed(self, message_id: str) -> None: ...

    def has_new(self) -> bool:
        """Cheap 'is there anything to do?' for the scheduled poller.

        Override where the answer can be had without downloading attachments - the
        worker asks this every tick, and a full poll on an empty mailbox is waste.
        """
        return bool(self.poll())

    def health(self) -> dict:
        return {"connector": self.name, "ok": True}


class SinkConnector(ABC):
    """Where the triaged workbook goes TO."""
    name: str = "sink"

    @abstractmethod
    def upload(self, local_path: str, remote_folder: str, metadata: dict | None = None) -> str:
        """Returns a shareable URL or path."""

    def health(self) -> dict:
        return {"connector": self.name, "ok": True}


class TicketConnector(ABC):
    """Where HIGH / MEDIUM cases become work items."""
    name: str = "ticket"

    @abstractmethod
    def create(self, *, summary: str, description: str, priority: str,
               labels: list[str], external_key: str, attachment_url: str | None = None) -> TicketRef: ...

    def find_existing(self, external_key: str) -> TicketRef | None:
        """Override to make ticket creation idempotent across reruns."""
        return None

    def health(self) -> dict:
        return {"connector": self.name, "ok": True}


# --------------------------------------------------------------------------- #
_REGISTRY: dict[str, dict[str, type]] = {"source": {}, "sink": {}, "ticket": {}}


def register(kind: str, key: str):
    def deco(cls):
        _REGISTRY[kind][key] = cls
        return cls
    return deco


def build(kind: str, key: str, cfg: dict):
    if key not in _REGISTRY[kind]:
        raise KeyError(f"Unknown {kind} connector '{key}'. Available: {sorted(_REGISTRY[kind])}")
    return _REGISTRY[kind][key](cfg)


def available(kind: str) -> list[str]:
    return sorted(_REGISTRY[kind])
