"""Tests for the direct CSV-to-blockchain ARK importer."""

import csv
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.cli.import_arks import (
    import_rows,
    main,
    parse_ark,
    read_csv,
)


def write_input(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "darkidentifier",
                "oaiidentifier",
                "datestamp",
                "itemurl",
                "lastmodified",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def input_row(identifier="ark:/41046/abc", url="https://example.org/item"):
    return {
        "darkidentifier": identifier,
        "oaiidentifier": "oai:example:1",
        "datestamp": "2025-07-31 12:28:05",
        "itemurl": url,
        "lastmodified": "2025-09-27 00:18:26",
    }


def orchestrator(authority_naans=None):
    mock = MagicMock()
    mock.get_authority_by_uuid.return_value = SimpleNamespace(
        active=True,
        wallet_address="0xABC",
        naans=authority_naans or ["41046"],
    )
    mock.is_authorized_for_naan.return_value = True
    mock.ark_exists.return_value = False
    mock.create_ark.return_value = SimpleNamespace(owner="0xABC")
    return mock


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ark:/41046/name", ("41046", "name")),
        ("ark:41046/name", ("41046", "name")),
    ],
)
def test_parse_ark_accepts_canonical_and_legacy_forms(value, expected):
    assert parse_ark(value) == expected


def test_read_csv_reports_missing_url_and_duplicate(tmp_path):
    source = tmp_path / "input.csv"
    write_input(
        source,
        [
            input_row(),
            input_row(identifier="ark:41046/abc"),
            input_row(identifier="ark:/48912/no-url", url=""),
        ],
    )

    valid, invalid = read_csv(source)

    assert [row.darkidentifier for row in valid] == ["ark:/41046/abc"]
    assert len(invalid) == 2
    assert "Duplicate ARK" in invalid[0].error
    assert "absolute HTTP(S) URL" in invalid[1].error


def test_preflight_aborts_before_writes_when_naan_is_not_authorized(tmp_path):
    source = tmp_path / "input.csv"
    results = tmp_path / "results.csv"
    write_input(source, [input_row()])
    rows, invalid = read_csv(source)
    mock = orchestrator()
    mock.is_authorized_for_naan.return_value = False

    with pytest.raises(RuntimeError, match="not authorized.*41046"):
        import_rows(
            orchestrator=mock,
            authority_id="authority-1",
            rows=rows,
            invalid_rows=invalid,
            results_path=results,
            execute=True,
        )

    mock.create_ark.assert_not_called()
    assert not results.exists()


def test_execute_creates_ark_with_empty_cid(tmp_path):
    source = tmp_path / "input.csv"
    results = tmp_path / "results.csv"
    write_input(source, [input_row()])
    rows, invalid = read_csv(source)
    mock = orchestrator()

    counts = import_rows(
        orchestrator=mock,
        authority_id="authority-1",
        rows=rows,
        invalid_rows=invalid,
        results_path=results,
        execute=True,
    )

    assert counts["created"] == 1
    mock.create_ark.assert_called_once_with(
        uuid="authority-1",
        naan="41046",
        name="abc",
        url="https://example.org/item",
        cid="",
    )
    assert "created" in results.read_text(encoding="utf-8")


def test_dry_run_validates_without_creating(tmp_path):
    source = tmp_path / "input.csv"
    results = tmp_path / "results.csv"
    write_input(source, [input_row()])
    rows, invalid = read_csv(source)
    mock = orchestrator()

    counts = import_rows(
        orchestrator=mock,
        authority_id="authority-1",
        rows=rows,
        invalid_rows=invalid,
        results_path=results,
        execute=False,
    )

    assert counts["validated"] == 1
    mock.create_ark.assert_not_called()
    assert "validated" in results.read_text(encoding="utf-8")


def test_existing_different_url_is_a_conflict_and_is_not_updated(tmp_path):
    source = tmp_path / "input.csv"
    results = tmp_path / "results.csv"
    write_input(source, [input_row()])
    rows, invalid = read_csv(source)
    mock = orchestrator()
    mock.ark_exists.return_value = True
    mock.get_ark.return_value = SimpleNamespace(
        owner="0xabc",
        url="https://example.org/other",
    )

    counts = import_rows(
        orchestrator=mock,
        authority_id="authority-1",
        rows=rows,
        invalid_rows=invalid,
        results_path=results,
        execute=True,
    )

    assert counts["conflict"] == 1
    mock.create_ark.assert_not_called()
    assert "conflict_url" in results.read_text(encoding="utf-8")


def test_existing_result_resumes_without_blockchain_lookup(tmp_path):
    source = tmp_path / "input.csv"
    results = tmp_path / "results.csv"
    write_input(source, [input_row()])
    results.write_text(
        "row,darkidentifier,itemurl,naan,name,status,owner,error\n"
        "2,ark:/41046/abc,https://example.org/item,41046,abc,created,0xABC,\n",
        encoding="utf-8",
    )
    rows, invalid = read_csv(source)
    mock = orchestrator()

    counts = import_rows(
        orchestrator=mock,
        authority_id="authority-1",
        rows=rows,
        invalid_rows=invalid,
        results_path=results,
        execute=True,
    )

    assert counts["resumed"] == 1
    mock.ark_exists.assert_not_called()
    mock.create_ark.assert_not_called()


def test_changed_url_does_not_resume_from_stale_result(tmp_path):
    source = tmp_path / "input.csv"
    results = tmp_path / "results.csv"
    write_input(source, [input_row(url="https://example.org/new")])
    results.write_text(
        "row,darkidentifier,itemurl,naan,name,status,owner,error\n"
        "2,ark:/41046/abc,https://example.org/old,41046,abc,created,0xABC,\n",
        encoding="utf-8",
    )
    rows, invalid = read_csv(source)
    mock = orchestrator()

    counts = import_rows(
        orchestrator=mock,
        authority_id="authority-1",
        rows=rows,
        invalid_rows=invalid,
        results_path=results,
        execute=True,
    )

    assert counts["created"] == 1
    mock.create_ark.assert_called_once()


def test_results_cannot_overwrite_input_csv(tmp_path, capsys):
    source = tmp_path / "input.csv"
    write_input(source, [input_row()])

    exit_code = main(
        [
            str(source),
            "--authority-id",
            "authority-1",
            "--results",
            str(source),
        ]
    )

    assert exit_code == 2
    assert "must not overwrite" in capsys.readouterr().err
