"""
Configuration settings for dARK Core API.

Uses pydantic-settings for environment variable loading and validation.
"""

from functools import lru_cache
from pathlib import Path
from typing import Optional
from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _resolve_env_file() -> str:
    """Prefer local .env.integration over .env for deployed test stacks."""
    project_root = Path(__file__).resolve().parents[1]
    integration_env = project_root / ".env.integration"
    default_env = project_root / ".env"

    if integration_env.exists():
        return str(integration_env)

    return str(default_env)


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
    )
    
    # API Server
    minter_api_host: str = Field(
        default="0.0.0.0",
        validation_alias=AliasChoices("MINTER_API_HOST", "CORE_API_HOST"),
    )
    minter_api_port: int = Field(
        default=8001,
        validation_alias=AliasChoices("MINTER_API_PORT", "CORE_API_PORT"),
    )
    minter_api_workers: int = 2
    
    # Batch processing
    batch_size_limit: int = 100
    
    # Database Configuration
    database_url: str = "postgresql://dark:dark_password@localhost:5433/minter"
    database_echo: bool = False
    database_pool_size: int = 5
    database_max_overflow: int = 10
    
    # Authorization Cache (LRU cache for NAAN validation)
    auth_cache_ttl: int = 60  # seconds
    auth_cache_maxsize: int = 1000
    
    # Worker Configuration
    metadata_worker_enabled: bool = True
    metadata_worker_page_size: int = 100
    metadata_worker_concurrency: int = 4
    metadata_worker_sleep_seconds: int = 2
    metadata_worker_storage_retry_seconds: int = 10
    metadata_worker_max_retries: int = 5
    metadata_worker_retry_backoff_base: float = 2.0
    metadata_worker_runtime_name: str = "metadata-publisher"

    chain_worker_enabled: bool = True
    chain_worker_page_size: int = 20
    chain_worker_sleep_seconds: int = 5
    chain_worker_rpc_retry_seconds: int = 10
    chain_worker_congestion_retry_seconds: int = 30
    chain_worker_block_stall_seconds: int = 120
    chain_worker_adaptive_page_enabled: bool = True
    chain_worker_min_page_size: int = 1
    chain_worker_recovery_success_cycles: int = 3
    chain_worker_max_retries: int = 5
    chain_worker_retry_backoff_base: float = 2.0
    chain_worker_runtime_name: str = "chain-publisher"
    worker_heartbeat_interval_seconds: int = 10
    worker_heartbeat_stale_after_seconds: int = 180
    permanent_rescue_max_items: int = 50
    
    # Metadata Storage Configuration
    metadata_storage_type: str = "store_api"  # "filesystem" or "store_api"
    metadata_storage_path: str = "./metadata_storage"  # Path for filesystem storage
    metadata_store_api_url: str = "http://localhost:8003"
    metadata_store_api_timeout_seconds: float = 10.0
    
    # Minter Configuration
    minter_shoulder: str = "200"
    minter_noid_length: int = 7
    minter_noid_checkdigit: bool = True
    
    # mTLS Configuration
    mtls_enabled: bool = False
    tls_cert_file: Optional[str] = None
    tls_key_file: Optional[str] = None
    tls_ca_file: Optional[str] = None
    
    # Blockchain connection shared with dark-core-lib
    dark_rpc_url: str = "http://localhost:8545"
    dark_rpc_health_timeout_seconds: float = 2.0
    dark_rpc_connect_retry_seconds: float = 30.0
    dark_rpc_connect_retry_interval_seconds: float = 2.0
    dark_chain_id: int = 1337
    dark_gas_limit: int = 550000
    dark_authority_address: str = ""
    dark_contract_address: str = ""
    dark_admin_private_key: str = ""

    @field_validator("minter_shoulder")
    @classmethod
    def validate_minter_shoulder(cls, value: str) -> str:
        """Require the DARK 2 shoulder format: version ``2`` plus minter code."""
        if len(value) != 3 or not value.isascii() or not value.isdigit() or not value.startswith("2"):
            raise ValueError(
                "MINTER_SHOULDER must be exactly three digits in the format 2MM "
                "(2 = DARK version; MM = minter code)"
            )
        return value

    def validate_blockchain_config(self) -> None:
        """Validate that blockchain configuration is complete."""
        missing = []
        if not self.dark_authority_address:
            missing.append("DARK_AUTHORITY_ADDRESS")
        if not self.dark_contract_address:
            missing.append("DARK_CONTRACT_ADDRESS")
        if not self.dark_admin_private_key:
            missing.append("DARK_ADMIN_PRIVATE_KEY")
        
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
    
    def validate_mtls_config(self) -> None:
        """Validate mTLS configuration when enabled."""
        if not self.mtls_enabled:
            return
        
        missing = []
        if not self.tls_cert_file:
            missing.append("TLS_CERT_FILE")
        if not self.tls_key_file:
            missing.append("TLS_KEY_FILE")
        if not self.tls_ca_file:
            missing.append("TLS_CA_FILE")
        
        if missing:
            raise ValueError(f"mTLS enabled but missing: {', '.join(missing)}")

    def validate_database_config(self) -> None:
        """Validate PostgreSQL-only database configuration."""
        if not self.database_url.startswith("postgresql"):
            raise ValueError(
                "DATABASE_URL must use PostgreSQL (postgresql://...) in this deployment."
            )


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings(_env_file=_resolve_env_file())
