"""
Configuration settings for dARK Core API.

Uses pydantic-settings for environment variable loading and validation.
"""

from functools import lru_cache
from pathlib import Path
from typing import Optional
from pydantic import AliasChoices, Field
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
    
    # Batch processing
    batch_size_limit: int = 100
    
    # Database Configuration
    database_url: str = "postgresql://dark:dark_password@localhost:5432/minter"
    database_echo: bool = False
    database_pool_size: int = 5
    database_max_overflow: int = 10
    
    # Authorization Cache (LRU cache for NAAN validation)
    auth_cache_ttl: int = 60  # seconds
    auth_cache_maxsize: int = 1000
    
    # Worker Configuration
    worker_enabled: bool = True
    worker_interval_seconds: int = 60  # Run every 60 seconds
    worker_batch_size: int = 10  # Process up to 10 ARKs per cycle
    worker_max_retries: int = 5  # Max retry attempts before marking as failed
    worker_retry_backoff_base: float = 2.0  # Exponential backoff base (seconds)
    worker_runtime_name: str = "ark-publisher"
    worker_heartbeat_interval_seconds: int = 10
    worker_heartbeat_stale_after_seconds: int = 180
    
    # Metadata Storage Configuration
    metadata_storage_type: str = "store_api"  # "filesystem" or "store_api"
    metadata_storage_path: str = "./metadata_storage"  # Path for filesystem storage
    metadata_store_api_url: str = "http://localhost:8003"
    metadata_store_api_timeout_seconds: float = 10.0
    
    # Minter Configuration
    minter_shoulder: str = ""
    minter_noid_length: int = 7
    minter_noid_checkdigit: bool = True
    
    # mTLS Configuration
    mtls_enabled: bool = False
    tls_cert_file: Optional[str] = None
    tls_key_file: Optional[str] = None
    tls_ca_file: Optional[str] = None
    
    # Blockchain connection shared with dark-core-lib
    dark_rpc_url: str = "http://localhost:8545"
    dark_chain_id: int = 1337
    dark_authority_address: str = ""
    dark_contract_address: str = ""
    dark_admin_private_key: str = ""
    
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
