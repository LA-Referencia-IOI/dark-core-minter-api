"""Test stub ARK models for dark_orchestrator."""

from dataclasses import dataclass
from datetime import datetime


@dataclass
class ARKInfo:
    """Minimal ARK info structure used in tests."""

    naan: str
    name: str
    url: str
    cid: str
    owner: str
    created_at: datetime
    updated_at: datetime

