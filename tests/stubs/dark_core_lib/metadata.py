from __future__ import annotations

"""Lightweight metadata stub for minter tests."""

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

import httpx
from pydantic import BaseModel, Field


class StorageError(Exception):
    pass


class MetadataNotFoundError(StorageError):
    pass


@dataclass(frozen=True)
class StoredDocument:
    content: bytes
    content_type: str
    schema: Optional[str] = None

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    @property
    def format(self) -> str:
        if "json" in self.content_type:
            return "json"
        if "xml" in self.content_type:
            return "xml"
        return "text"


@dataclass(frozen=True)
class ReplicationStatus:
    cid: str
    status: str
    total_replicas: int
    local_replicas: int
    remote_replicas: int
    sites: dict[str, int] = field(default_factory=dict)
    purge_target_met: bool = False
    checked_at: Optional[datetime] = None


class MetadataStorage(ABC):
    @abstractmethod
    def store_document(self, content: bytes, content_type: str, schema: Optional[str] = None) -> str:
        raise NotImplementedError

    @abstractmethod
    def get_document(self, cid: str) -> StoredDocument:
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> bool:
        raise NotImplementedError

    def store_metadata(self, content: str, format: str) -> str:
        content_type = "application/json" if format == "json" else "application/xml" if format == "xml" else "text/plain"
        return self.store_document(content.encode("utf-8"), content_type)

    def get_metadata(self, cid: str) -> tuple[str, str]:
        document = self.get_document(cid)
        return document.text, document.format

    def get_replication_status(self, cid: str) -> ReplicationStatus:
        self.get_document(cid)
        return ReplicationStatus(cid, "pinned", 1, 1, 0, {"local": 1}, True)

    def close(self) -> None:
        return None


class FileSystemMetadataStorage(MetadataStorage):
    def __init__(self, storage_path: str):
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)

    def _sanitize_cid(self, cid: str) -> str:
        safe_cid = Path(cid).name
        if safe_cid != cid:
            raise StorageError(f"Invalid CID format: {cid}")
        return safe_cid

    def _extension(self, content_type: str) -> str:
        if "json" in content_type:
            return ".json"
        if "xml" in content_type:
            return ".xml"
        return ".txt"

    def store_document(self, content: bytes, content_type: str, schema: Optional[str] = None) -> str:
        cid = hashlib.md5(content).hexdigest()
        base = self.storage_path / self._sanitize_cid(cid)
        (base.with_suffix(self._extension(content_type))).write_bytes(content)
        (base.with_suffix(".meta")).write_text(content_type, encoding="utf-8")
        return cid

    def get_document(self, cid: str) -> StoredDocument:
        base = self.storage_path / self._sanitize_cid(cid)
        meta_path = base.with_suffix(".meta")
        if not meta_path.exists():
            raise MetadataNotFoundError(f"Metadata not found for CID: {cid}")
        content_type = meta_path.read_text(encoding="utf-8")
        content_path = base.with_suffix(self._extension(content_type))
        if not content_path.exists():
            raise MetadataNotFoundError(f"Metadata not found for CID: {cid}")
        return StoredDocument(content=content_path.read_bytes(), content_type=content_type)

    def health_check(self) -> bool:
        try:
            if not self.storage_path.exists():
                return False
            test_file = self.storage_path / ".health_check"
            test_file.touch()
            test_file.unlink()
            return True
        except Exception:
            return False


class StoreApiMetadataStorage(MetadataStorage):
    def __init__(self, base_url: str, timeout_seconds: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.client = httpx.Client(timeout=timeout_seconds)

    def store_document(self, content: bytes, content_type: str, schema: Optional[str] = None) -> str:
        headers = {"Content-Type": content_type}
        try:
            response = self.client.post(
                f"{self.base_url}/v1/store",
                content=content,
                headers=headers,
            )
        except httpx.RequestError as exc:
            raise StorageError(f"Store API request failed: {exc}") from exc

        if response.status_code != 200:
            detail = response.json().get("detail", f"HTTP {response.status_code}")
            raise StorageError(f"Store API store failed ({response.status_code}): {detail}")
        payload = response.json()
        cid = payload.get("cid")
        if not cid:
            raise StorageError("Store API response missing CID")
        return cid

    def get_document(self, cid: str) -> StoredDocument:
        try:
            response = self.client.get(f"{self.base_url}/v1/retrieve/{cid}")
        except httpx.RequestError as exc:
            raise StorageError(f"Store API request failed: {exc}") from exc

        if response.status_code == 404:
            raise MetadataNotFoundError(f"Metadata not found for CID: {cid}")
        if response.status_code != 200:
            detail = response.json().get("detail", f"HTTP {response.status_code}")
            raise StorageError(f"Store API retrieve failed ({response.status_code}): {detail}")

        return StoredDocument(content=response.content, content_type="application/octet-stream")

    def health_check(self) -> bool:
        try:
            response = self.client.get(f"{self.base_url}/health")
        except httpx.RequestError:
            return False
        if response.status_code != 200:
            return False
        payload = response.json()
        if "backend_healthy" in payload:
            return bool(payload["backend_healthy"])
        return payload.get("status") == "healthy"

    def get_replication_status(self, cid: str) -> ReplicationStatus:
        try:
            response = self.client.get(f"{self.base_url}/v1/status/{cid}")
        except httpx.RequestError as exc:
            raise StorageError(f"Store API request failed: {exc}") from exc
        if response.status_code == 404:
            return ReplicationStatus(cid, "unpinned", 0, 0, 0)
        if response.status_code != 200:
            raise StorageError(f"Store API status failed ({response.status_code})")
        payload = response.json()
        replication = payload["replication"]
        return ReplicationStatus(
            cid=payload["cid"],
            status=payload["status"],
            total_replicas=replication["total_replicas"],
            local_replicas=replication["local_replicas"],
            remote_replicas=replication["remote_replicas"],
            sites=replication.get("sites", {}),
            purge_target_met=replication["purge_target_met"],
        )

    def close(self) -> None:
        self.client.close()


class OriginalMetadataRef(BaseModel):
    schema_: str = Field(..., alias="schema")
    media_type: str
    cid: Optional[str] = None

    model_config = {"populate_by_name": True}


class Level1Metadata(BaseModel):
    schema_uri: str = Field(default="https://dark.la-referencia.info/schemas/ark-metadata/v1", alias="$schema")
    schema_version: str = Field(default="1.0")
    ark: Optional[str] = None
    title: str
    authors: list[str]
    year: int
    publisher: Optional[str] = None
    resource_type: Optional[str] = None
    language: Optional[str] = None
    abstract: Optional[str] = None
    subjects: Optional[list[str]] = None
    rights: Optional[str] = None
    alternate_identifiers: Optional[list[dict]] = None
    alternate_urls: Optional[list[str]] = None
    original_metadata: OriginalMetadataRef

    model_config = {"populate_by_name": True}


class MetadataService:
    def __init__(self, storage: MetadataStorage):
        self.storage = storage

    def store_level2(self, content: bytes, content_type: str, schema: Optional[str] = None) -> str:
        return self.storage.store_document(content=content, content_type=content_type, schema=schema)

    def load_level1(self, level1_cid: str) -> Level1Metadata:
        document = self.storage.get_document(level1_cid)
        return Level1Metadata.model_validate_json(document.content)

    def load_level2(self, level1: Level1Metadata) -> StoredDocument:
        document = self.storage.get_document(level1.original_metadata.cid)
        return StoredDocument(
            content=document.content,
            content_type=level1.original_metadata.media_type,
            schema=level1.original_metadata.schema_,
        )

    def with_level2_reference(self, level1: dict, level2_cid: str) -> Level1Metadata:
        payload = dict(level1)
        original_metadata = dict(payload.get("original_metadata") or {})
        original_metadata["cid"] = level2_cid
        payload["original_metadata"] = original_metadata
        return Level1Metadata.model_validate(payload)

    def store_level1(self, level1: Union[Level1Metadata, dict]) -> str:
        model = level1 if isinstance(level1, Level1Metadata) else Level1Metadata.model_validate(level1)
        return self.storage.store_document(
            content=model.model_dump_json(by_alias=True).encode("utf-8"),
            content_type="application/json",
        )

    def store_level1_with_level2_reference(self, level1: dict, level2_cid: str):
        model = self.with_level2_reference(level1, level2_cid)
        return model, self.store_level1(model)


def get_metadata_storage(storage_type: str = "filesystem", **kwargs) -> MetadataStorage:
    normalized = storage_type.lower()
    if normalized == "filesystem":
        return FileSystemMetadataStorage(kwargs.get("storage_path", "./metadata_storage"))
    if normalized == "store_api":
        return StoreApiMetadataStorage(
            kwargs.get("store_api_url", "http://localhost:8003"),
            timeout_seconds=kwargs.get("timeout_seconds", 10.0),
        )
    raise ValueError(f"Unsupported storage type: {storage_type}")
