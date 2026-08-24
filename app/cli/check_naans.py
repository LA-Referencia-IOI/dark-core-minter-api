"""Extract NAANs from an ARK CSV and optionally check authority assignments."""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, TextIO

from app.cli.import_arks import build_orchestrator, parse_ark


@dataclass(frozen=True)
class NaanScan:
    """Unique NAANs plus malformed ARK rows found during a CSV scan."""

    naans: tuple[str, ...]
    invalid_rows: tuple[tuple[int, str, str], ...]


def scan_naans(path: Path) -> NaanScan:
    """Extract unique sorted NAANs from the ``darkidentifier`` CSV column."""

    naans: set[str] = set()
    invalid_rows: list[tuple[int, str, str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if "darkidentifier" not in set(reader.fieldnames or []):
            raise ValueError("CSV is missing required column: darkidentifier")

        for row_number, row in enumerate(reader, start=2):
            identifier = (row.get("darkidentifier") or "").strip()
            try:
                naan, _ = parse_ark(identifier)
                naans.add(naan)
            except ValueError as exc:
                invalid_rows.append((row_number, identifier, str(exc)))

    return NaanScan(
        naans=tuple(sorted(naans)),
        invalid_rows=tuple(invalid_rows),
    )


def check_authority_naans(orchestrator, authority_id: str, naans: tuple[str, ...]):
    """Return authority information and an authorization map for every NAAN."""

    authority = orchestrator.get_authority_by_uuid(authority_id)
    authorization = {
        naan: bool(orchestrator.is_authorized_for_naan(authority_id, naan))
        for naan in naans
    }
    return authority, authorization


def emit_rows(
    rows: list[dict[str, str]],
    *,
    output_format: str,
    output: TextIO,
) -> None:
    """Render results as a human-readable table or CSV."""

    if output_format == "csv":
        fieldnames = ["naan", "associated"] if rows and "associated" in rows[0] else ["naan"]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        return

    if not rows:
        output.write("No NAANs found.\n")
        return
    if "associated" not in rows[0]:
        for row in rows:
            output.write(f"{row['naan']}\n")
        return

    output.write(f"{'NAAN':<20} ASSOCIATED\n")
    for row in rows:
        output.write(f"{row['naan']:<20} {row['associated']}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dark-check-naans",
        description="List CSV NAANs and optionally verify their authority assignments.",
    )
    parser.add_argument("csv_file", type=Path, help="CSV containing darkidentifier")
    parser.add_argument("--authority-id", help="Authority UUID to verify")
    parser.add_argument("--env-file", type=Path, help="Minter-compatible .env file")
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="With --authority-id, print only NAANs not assigned to the authority.",
    )
    parser.add_argument(
        "--format",
        choices=("table", "csv"),
        default="table",
        help="Output format (default: table).",
    )
    parser.add_argument("--output", type=Path, help="Write output to a file instead of stdout")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.csv_file.is_file():
        print(f"error: CSV file not found: {args.csv_file}", file=sys.stderr)
        return 2
    if args.env_file is not None and not args.env_file.is_file():
        print(f"error: env file not found: {args.env_file}", file=sys.stderr)
        return 2
    if args.only_missing and not args.authority_id:
        print("error: --only-missing requires --authority-id", file=sys.stderr)
        return 2
    if args.output and args.output.resolve() == args.csv_file.resolve():
        print("error: --output must not overwrite the input CSV", file=sys.stderr)
        return 2

    try:
        scan = scan_naans(args.csv_file)
        if not scan.naans and not scan.invalid_rows:
            raise ValueError("CSV contains no data rows")

        exit_code = 1 if scan.invalid_rows else 0
        if args.authority_id:
            orchestrator = build_orchestrator(args.env_file)
            authority, authorization = check_authority_naans(
                orchestrator,
                args.authority_id,
                scan.naans,
            )
            if not authority.active:
                print(f"warning: authority {args.authority_id} is inactive", file=sys.stderr)
                exit_code = 1
            missing = [naan for naan, associated in authorization.items() if not associated]
            if missing:
                exit_code = 1
            selected = missing if args.only_missing else list(scan.naans)
            rows = [
                {"naan": naan, "associated": "yes" if authorization[naan] else "no"}
                for naan in selected
            ]
        else:
            rows = [{"naan": naan} for naan in scan.naans]

        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("w", encoding="utf-8", newline="") as handle:
                emit_rows(rows, output_format=args.format, output=handle)
        else:
            emit_rows(rows, output_format=args.format, output=sys.stdout)

        for row_number, identifier, error in scan.invalid_rows:
            print(
                f"warning: CSV row {row_number}: {identifier or '<empty>'}: {error}",
                file=sys.stderr,
            )
        return exit_code
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
