"""Test stub authority models for dark_orchestrator."""

from dataclasses import dataclass


@dataclass
class AuthorityInfo:
    """Minimal authority info structure used in tests."""

    uuid: str
    wallet_address: str
    naans: list[str]
    active: bool

