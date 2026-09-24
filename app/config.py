"""
Configuration settings for dARK Core API.

Uses pydantic-settings for environment variable loading and validation.
"""

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional
from pydantic import AliasChoices, Field, field_validator, model_validator
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

    # Logging. Containers log to stdout by default; an optional rotating file
    # handler is available only when an operator explicitly configures it.
    minter_log_level: str = "INFO"
    minter_log_json: bool = False
    minter_log_file: Optional[str] = None
    minter_log_file_max_bytes: int = 10 * 1024 * 1024
    minter_log_file_backup_count: int = 5
    minter_log_warning_repeat_seconds: int = 300
    minter_uvicorn_access_log: bool = False
    
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
    metadata_worker_min_concurrency: int = 2
    metadata_worker_sleep_seconds: int = 2
    metadata_worker_storage_retry_seconds: int = 10
    metadata_worker_max_retries: int = 5
    metadata_worker_retry_backoff_base: float = 2.0
    metadata_worker_runtime_name: str = "metadata-publisher"

    replication_worker_enabled: bool = True
    replication_worker_page_size: int = 100
    replication_worker_concurrency: int = 2
    replication_worker_sleep_seconds: int = 2
    # First-pin confirmation controls the critical path to Chain.  It is
    # deliberately sparse: 15 seconds, then one minute, then five minutes.
    replication_first_pin_recheck_seconds: int = 15
    replication_first_pin_second_recheck_seconds: int = 60
    replication_first_pin_max_recheck_seconds: int = 300
    # Final durability is asynchronous Cluster maintenance.  Once requested,
    # it is audited at five minutes, fifteen minutes and then hourly.
    replication_durability_recheck_seconds: int = 300
    replication_durability_second_recheck_seconds: int = 900
    replication_durability_max_recheck_seconds: int = 3600
    replication_repair_grace_seconds: int = 120
    replication_repair_cooldown_seconds: int = 900
    replication_worker_storage_retry_seconds: int = 10
    replication_status_batch_size: int = 200
    # Durability is a maintenance operation.  Limit how many CIDs may be
    # promoted in one maintenance pass so a large published backlog cannot
    # flood IPFS Cluster while it is still pinning earlier requests.
    replication_promotion_batch_size: int = 100
    replication_promotion_pressure_high_percent: int = 80
    replication_promotion_pressure_medium_percent: int = 50
    replication_promotion_min_batch_size: int = 20
    # Durability is deliberately paced.  First-pin availability remains
    # immediate, while maintenance leaves room for Cluster's asynchronous
    # pin tracker to complete the allocations it already accepted.
    replication_maintenance_cycle_seconds: int = 5
    replication_idle_sleep_seconds: int = 2
    replication_worker_runtime_name: str = "replication-reconciler"
    replication_publish_after_replicas: int = 1
    replication_target_replicas: int = 2
    worker_max_idle_sleep_seconds: int = 10

    chain_worker_enabled: bool = True
    # Claim a larger database page, but bound each authority's RPC/nonce
    # pipeline so a single call never overloads Besu.
    chain_worker_page_size: int = 100
    chain_worker_rpc_batch_size: int = 50
    chain_worker_sleep_seconds: int = 5
    chain_worker_rpc_retry_seconds: int = 10
    chain_worker_congestion_retry_seconds: int = 30
    chain_worker_block_stall_seconds: int = 120
    chain_worker_adaptive_page_enabled: bool = True
    chain_worker_min_page_size: int = 1
    chain_worker_healthy_cycles_before_growing: int = 3
    chain_worker_max_retries: int = 5
    chain_worker_retry_backoff_base: float = 2.0
    chain_worker_runtime_name: str = "chain-publisher"
    worker_heartbeat_interval_seconds: int = 10
    worker_heartbeat_stale_after_seconds: int = 180
    
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
        """Require the DARK 2 shoulder: ``2`` plus two lowercase alphanumerics."""
        if not re.fullmatch(r"2[0-9a-z]{2}", value):
            raise ValueError(
                "MINTER_SHOULDER must use the 2xx format "
                "(2 = DARK version; x = lowercase alphanumeric minter code)"
            )
        return value

    @field_validator("minter_log_level")
    @classmethod
    def validate_minter_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("MINTER_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        return normalized

    @model_validator(mode="after")
    def validate_replication_thresholds(self) -> "Settings":
        """Require a publication threshold no greater than the durability target."""
        if self.replication_publish_after_replicas < 1:
            raise ValueError("REPLICATION_PUBLISH_AFTER_REPLICAS must be at least 1")
        if self.replication_target_replicas < self.replication_publish_after_replicas:
            raise ValueError(
                "REPLICATION_TARGET_REPLICAS must be greater than or equal to "
                "REPLICATION_PUBLISH_AFTER_REPLICAS"
            )
        for field_name in (
            "replication_first_pin_recheck_seconds", "replication_first_pin_second_recheck_seconds",
            "replication_first_pin_max_recheck_seconds", "replication_durability_recheck_seconds",
            "replication_durability_second_recheck_seconds", "replication_durability_max_recheck_seconds",
            "replication_repair_grace_seconds", "replication_repair_cooldown_seconds",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name.upper()} cannot be negative")
        if not (
            self.replication_first_pin_recheck_seconds
            <= self.replication_first_pin_second_recheck_seconds
            <= self.replication_first_pin_max_recheck_seconds
        ):
            raise ValueError("first-pin replication rechecks must be nondecreasing")
        if not (
            self.replication_durability_recheck_seconds
            <= self.replication_durability_second_recheck_seconds
            <= self.replication_durability_max_recheck_seconds
        ):
            raise ValueError("durability replication rechecks must be nondecreasing")
        if self.metadata_worker_min_concurrency < 1 or self.metadata_worker_min_concurrency > self.metadata_worker_concurrency:
            raise ValueError("METADATA_WORKER_MIN_CONCURRENCY must be between 1 and METADATA_WORKER_CONCURRENCY")
        if self.replication_status_batch_size < 1 or self.replication_status_batch_size > 200:
            raise ValueError("REPLICATION_STATUS_BATCH_SIZE must be between 1 and 200")
        if self.replication_promotion_batch_size < 1 or self.replication_promotion_batch_size > 200:
            raise ValueError("REPLICATION_PROMOTION_BATCH_SIZE must be between 1 and 200")
        if not (0 < self.replication_promotion_pressure_medium_percent < self.replication_promotion_pressure_high_percent <= 100):
            raise ValueError("replication promotion pressure thresholds must satisfy 0 < medium < high <= 100")
        if self.replication_promotion_min_batch_size < 1 or self.replication_promotion_min_batch_size > self.replication_promotion_batch_size:
            raise ValueError("REPLICATION_PROMOTION_MIN_BATCH_SIZE must be between 1 and REPLICATION_PROMOTION_BATCH_SIZE")
        if self.worker_max_idle_sleep_seconds < 1:
            raise ValueError("WORKER_MAX_IDLE_SLEEP_SECONDS must be at least 1")
        if self.replication_maintenance_cycle_seconds < 1:
            raise ValueError("REPLICATION_MAINTENANCE_CYCLE_SECONDS must be at least 1")
        if self.replication_idle_sleep_seconds < 1:
            raise ValueError("REPLICATION_IDLE_SLEEP_SECONDS must be at least 1")
        if self.chain_worker_page_size < 1:
            raise ValueError("CHAIN_WORKER_PAGE_SIZE must be at least 1")
        if self.chain_worker_rpc_batch_size < 1:
            raise ValueError("CHAIN_WORKER_RPC_BATCH_SIZE must be at least 1")
        if self.chain_worker_rpc_batch_size > self.chain_worker_page_size:
            raise ValueError(
                "CHAIN_WORKER_RPC_BATCH_SIZE cannot exceed CHAIN_WORKER_PAGE_SIZE"
            )
        if self.minter_log_file_max_bytes < 1:
            raise ValueError("MINTER_LOG_FILE_MAX_BYTES must be at least 1")
        if self.minter_log_file_backup_count < 0:
            raise ValueError("MINTER_LOG_FILE_BACKUP_COUNT cannot be negative")
        if self.minter_log_warning_repeat_seconds < 1:
            raise ValueError("MINTER_LOG_WARNING_REPEAT_SECONDS must be at least 1")
        return self

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
