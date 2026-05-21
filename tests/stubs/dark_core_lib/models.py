"""Test stub models for dark_core_lib."""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


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


@dataclass
class AuthorityInfo:
    """Minimal authority info structure used in tests."""

    uuid: str
    wallet_address: str
    naans: list[str]
    active: bool


@dataclass
class TxReceiptInfo:
    """Minimal transaction receipt structure used in tests."""

    tx_hash: str
    status: int
    gas_used: Optional[int]
    block_number: Optional[int]


@dataclass
class ARKPublishOperation:
    """Minimal semantic ARK publish operation used in tests."""

    ref: str
    action: str
    naan: str
    name: str
    url: str
    cid: str


@dataclass
class ARKPublishResult:
    """Minimal semantic ARK publish result used in tests."""

    ref: str
    action: str
    status: str
    error: Optional[str] = None
