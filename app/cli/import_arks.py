"""Import ARKs directly from CSV into the dARK contract.

This command deliberately bypasses the Minter HTTP API and PostgreSQL. It uses
the same ``dark_core_lib`` library as the Minter service and creates ARKs
without metadata (an empty CID).
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, TextIO
from urllib.parse import urlparse


ARK_PATTERN = re.compile(r"^ark:/?([^/]+)/(.+)$")
REQUIRED_COLUMNS = {"darkidentifier", "itemurl"}
RESULT_COLUMNS = [
    "row",
    "darkidentifier",
    "itemurl",
    "naan",
    "name",
    "status",
    "owner",
    "error",
]
RESUMABLE_STATUSES = {"created", "skipped_same_url"}


@dataclass(frozen=True)
class ImportRow:
    """Normalized input row ready for preflight or publication."""

    row_number: int
    darkidentifier: str
    itemurl: str
    naan: str
    name: str


@dataclass(frozen=True)
class InvalidRow:
    """Input row that cannot be imported."""

    row_number: int
    darkidentifier: str
    itemurl: str
    error: str


def parse_ark(identifier: str) -> tuple[str, str]:
    """Parse ``ark:/NAAN/name`` and the legacy ``ark:NAAN/name`` form."""

    value = identifier.strip()
    match = ARK_PATTERN.fullmatch(value)
    if not match:
        raise ValueError("Expected ARK in the form ark:/NAAN/name")

    naan, name = match.groups()
    if not naan.strip() or not name.strip():
        raise ValueError("ARK NAAN and name must not be empty")
    if any(character.isspace() for character in naan + name):
        raise ValueError("ARK NAAN and name must not contain whitespace")
    return naan, name


def validate_url(value: str) -> str:
    """Return a normalized HTTP(S) URL or raise ``ValueError``."""

    url = value.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("itemurl must be an absolute HTTP(S) URL")
    return url


def read_csv(path: Path, limit: Optional[int] = None) -> tuple[list[ImportRow], list[InvalidRow]]:
    """Read and normalize the import file before any blockchain write."""

    rows: list[ImportRow] = []
    invalid: list[InvalidRow] = []
    seen: dict[tuple[str, str], int] = {}

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = set(reader.fieldnames or [])
        missing = sorted(REQUIRED_COLUMNS - columns)
        if missing:
            raise ValueError(f"CSV is missing required columns: {', '.join(missing)}")

        for row_number, raw in enumerate(reader, start=2):
            if limit is not None and len(rows) + len(invalid) >= limit:
                break

            identifier = (raw.get("darkidentifier") or "").strip()
            itemurl = (raw.get("itemurl") or "").strip()
            try:
                naan, name = parse_ark(identifier)
                itemurl = validate_url(itemurl)
                key = (naan, name)
                if key in seen:
                    raise ValueError(f"Duplicate ARK; first occurrence is CSV row {seen[key]}")
                seen[key] = row_number
                rows.append(
                    ImportRow(
                        row_number=row_number,
                        darkidentifier=f"ark:/{naan}/{name}",
                        itemurl=itemurl,
                        naan=naan,
                        name=name,
                    )
                )
            except ValueError as exc:
                invalid.append(
                    InvalidRow(
                        row_number=row_number,
                        darkidentifier=identifier,
                        itemurl=itemurl,
                        error=str(exc),
                    )
                )

    return rows, invalid


def load_completed(path: Path) -> dict[str, tuple[str, str]]:
    """Load completed ARKs and their URL/owner from an existing checkpoint."""

    if not path.exists() or path.stat().st_size == 0:
        return {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        return {
            (row.get("darkidentifier") or "").strip(): (
                (row.get("itemurl") or "").strip(),
                (row.get("owner") or "").strip().lower(),
            )
            for row in csv.DictReader(handle)
            if row.get("status") in RESUMABLE_STATUSES
        }


def result_writer(path: Path) -> tuple[TextIO, csv.DictWriter]:
    """Open the checkpoint/result CSV and write its header when necessary."""

    path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not path.exists() or path.stat().st_size == 0
    handle = path.open("a", encoding="utf-8", newline="")
    writer = csv.DictWriter(handle, fieldnames=RESULT_COLUMNS)
    if needs_header:
        writer.writeheader()
        handle.flush()
    return handle, writer


def write_result(
    handle: TextIO,
    writer: csv.DictWriter,
    *,
    row_number: int,
    darkidentifier: str,
    itemurl: str,
    naan: str = "",
    name: str = "",
    status: str,
    owner: str = "",
    error: str = "",
) -> None:
    """Append and flush one checkpoint row."""

    writer.writerow(
        {
            "row": row_number,
            "darkidentifier": darkidentifier,
            "itemurl": itemurl,
            "naan": naan,
            "name": name,
            "status": status,
            "owner": owner,
            "error": error,
        }
    )
    handle.flush()


def build_orchestrator(env_file: Optional[Path]):
    """Build the same blockchain client used by the Minter service."""

    from dark_core_lib import DARKCoreClient

    return DARKCoreClient.from_env(env_path=str(env_file) if env_file else None)


def preflight_authority(orchestrator, authority_id: str, naans: Iterable[str]):
    """Verify authority existence, activity and every required NAAN up front."""

    authority = orchestrator.get_authority_by_uuid(authority_id)
    if not authority.active:
        raise RuntimeError(f"Authority {authority_id} is inactive")

    unauthorized = sorted(
        naan for naan in set(naans)
        if not orchestrator.is_authorized_for_naan(authority_id, naan)
    )
    if unauthorized:
        raise RuntimeError(
            f"Authority {authority_id} is not authorized for NAAN(s): "
            + ", ".join(unauthorized)
        )
    return authority


def import_rows(
    *,
    orchestrator,
    authority_id: str,
    rows: list[ImportRow],
    invalid_rows: list[InvalidRow],
    results_path: Path,
    execute: bool,
) -> dict[str, int]:
    """Validate ownership/existence and optionally create all valid ARKs."""

    authority = preflight_authority(orchestrator, authority_id, (row.naan for row in rows))
    authority_wallet = authority.wallet_address.lower()
    completed = load_completed(results_path) if execute else {}
    counts = {
        "created": 0,
        "validated": 0,
        "skipped": 0,
        "conflict": 0,
        "invalid": len(invalid_rows),
        "failed": 0,
        "resumed": 0,
    }

    handle, writer = result_writer(results_path)
    try:
        for invalid in invalid_rows:
            write_result(
                handle,
                writer,
                row_number=invalid.row_number,
                darkidentifier=invalid.darkidentifier,
                itemurl=invalid.itemurl,
                status="invalid",
                error=invalid.error,
            )

        for row in rows:
            completed_url, completed_owner = completed.get(row.darkidentifier, (None, None))
            if completed_url == row.itemurl and completed_owner == authority_wallet:
                counts["resumed"] += 1
                continue

            try:
                if orchestrator.ark_exists(row.naan, row.name):
                    existing = orchestrator.get_ark(row.naan, row.name)
                    existing_owner = (existing.owner or "").lower()
                    if existing_owner != authority_wallet:
                        counts["conflict"] += 1
                        write_result(
                            handle,
                            writer,
                            row_number=row.row_number,
                            darkidentifier=row.darkidentifier,
                            itemurl=row.itemurl,
                            naan=row.naan,
                            name=row.name,
                            status="conflict_owner",
                            owner=existing.owner,
                            error="ARK is owned by another wallet",
                        )
                    elif existing.url != row.itemurl:
                        counts["conflict"] += 1
                        write_result(
                            handle,
                            writer,
                            row_number=row.row_number,
                            darkidentifier=row.darkidentifier,
                            itemurl=row.itemurl,
                            naan=row.naan,
                            name=row.name,
                            status="conflict_url",
                            owner=existing.owner,
                            error=f"Existing URL: {existing.url}",
                        )
                    else:
                        counts["skipped"] += 1
                        write_result(
                            handle,
                            writer,
                            row_number=row.row_number,
                            darkidentifier=row.darkidentifier,
                            itemurl=row.itemurl,
                            naan=row.naan,
                            name=row.name,
                            status="skipped_same_url",
                            owner=existing.owner,
                        )
                    continue

                if not execute:
                    counts["validated"] += 1
                    write_result(
                        handle,
                        writer,
                        row_number=row.row_number,
                        darkidentifier=row.darkidentifier,
                        itemurl=row.itemurl,
                        naan=row.naan,
                        name=row.name,
                        status="validated",
                        owner=authority.wallet_address,
                    )
                    continue

                created = orchestrator.create_ark(
                    uuid=authority_id,
                    naan=row.naan,
                    name=row.name,
                    url=row.itemurl,
                    cid="",
                )
                counts["created"] += 1
                write_result(
                    handle,
                    writer,
                    row_number=row.row_number,
                    darkidentifier=row.darkidentifier,
                    itemurl=row.itemurl,
                    naan=row.naan,
                    name=row.name,
                    status="created",
                    owner=created.owner,
                )
            except Exception as exc:  # Keep a large migration resumable after one bad row.
                counts["failed"] += 1
                write_result(
                    handle,
                    writer,
                    row_number=row.row_number,
                    darkidentifier=row.darkidentifier,
                    itemurl=row.itemurl,
                    naan=row.naan,
                    name=row.name,
                    status="failed",
                    error=str(exc),
                )
    finally:
        handle.close()
    return counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dark-import-arks",
        description="Import CSV ARKs directly to blockchain without metadata.",
    )
    parser.add_argument("csv_file", type=Path, help="CSV containing darkidentifier and itemurl")
    parser.add_argument("--authority-id", required=True, help="Registered authority UUID")
    parser.add_argument("--env-file", type=Path, help="Minter-compatible .env file")
    parser.add_argument(
        "--results",
        type=Path,
        help="Checkpoint CSV (default: <input>.import-results.csv)",
    )
    parser.add_argument("--limit", type=int, help="Process at most this many CSV rows")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Submit transactions. Without this flag the command is a dry run.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        print("error: --limit must be greater than zero", file=sys.stderr)
        return 2
    if not args.csv_file.is_file():
        print(f"error: CSV file not found: {args.csv_file}", file=sys.stderr)
        return 2
    if args.env_file is not None and not args.env_file.is_file():
        print(f"error: env file not found: {args.env_file}", file=sys.stderr)
        return 2

    results_path = args.results or args.csv_file.with_suffix(".import-results.csv")
    if results_path.resolve() == args.csv_file.resolve():
        print("error: --results must not overwrite the input CSV", file=sys.stderr)
        return 2
    try:
        rows, invalid_rows = read_csv(args.csv_file, args.limit)
        if not rows and not invalid_rows:
            raise ValueError("CSV contains no data rows")

        orchestrator = build_orchestrator(args.env_file)
        counts = import_rows(
            orchestrator=orchestrator,
            authority_id=args.authority_id,
            rows=rows,
            invalid_rows=invalid_rows,
            results_path=results_path,
            execute=args.execute,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    mode = "EXECUTE" if args.execute else "DRY RUN"
    summary = " ".join(f"{key}={value}" for key, value in counts.items())
    print(f"{mode}: {summary}")
    print(f"Results: {results_path}")
    return 1 if counts["invalid"] or counts["conflict"] or counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
