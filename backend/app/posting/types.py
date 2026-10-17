"""Shared types for ERP connectors (kept separate so connector modules can import it without import cycles)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Rendered:
    filename: str
    content: bytes
    content_type: str
