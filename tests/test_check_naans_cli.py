"""Tests for CSV NAAN extraction and authority verification."""

import csv
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.cli.check_naans import main, scan_naans


def write_input(path, identifiers):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["darkidentifier", "itemurl"])
        writer.writeheader()
        for identifier in identifiers:
            writer.writerow({"darkidentifier": identifier, "itemurl": ""})


def test_scan_lists_unique_sorted_naans_without_requiring_urls(tmp_path):
    source = tmp_path / "input.csv"
    write_input(
        source,
        [
            "ark:/48912/name-1",
            "ark:/41046/name-2",
            "ark:41046/name-3",
        ],
    )

    scan = scan_naans(source)

    assert scan.naans == ("41046", "48912")
    assert scan.invalid_rows == ()


def test_without_authority_prints_only_naans(tmp_path, capsys):
    source = tmp_path / "input.csv"
    write_input(source, ["ark:/48912/a", "ark:/41046/b"])

    exit_code = main([str(source)])

    assert exit_code == 0
    assert capsys.readouterr().out.splitlines() == ["41046", "48912"]


def test_authority_check_prints_association_and_returns_one_for_missing(tmp_path, capsys):
    source = tmp_path / "input.csv"
    env_file = tmp_path / ".env"
    env_file.write_text("test=true\n", encoding="utf-8")
    write_input(source, ["ark:/41046/a", "ark:/48912/b"])
    orchestrator = MagicMock()
    orchestrator.get_authority_by_uuid.return_value = SimpleNamespace(active=True)
    orchestrator.is_authorized_for_naan.side_effect = lambda _, naan: naan == "41046"

    with patch("app.cli.check_naans.build_orchestrator", return_value=orchestrator):
        exit_code = main(
            [
                str(source),
                "--authority-id",
                "authority-1",
                "--env-file",
                str(env_file),
            ]
        )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "41046" in output and "yes" in output
    assert "48912" in output and "no" in output


def test_only_missing_outputs_only_unassigned_naans_as_csv(tmp_path, capsys):
    source = tmp_path / "input.csv"
    write_input(source, ["ark:/41046/a", "ark:/48912/b"])
    orchestrator = MagicMock()
    orchestrator.get_authority_by_uuid.return_value = SimpleNamespace(active=True)
    orchestrator.is_authorized_for_naan.side_effect = lambda _, naan: naan == "41046"

    with patch("app.cli.check_naans.build_orchestrator", return_value=orchestrator):
        exit_code = main(
            [
                str(source),
                "--authority-id",
                "authority-1",
                "--only-missing",
                "--format",
                "csv",
            ]
        )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert output.splitlines() == ["naan,associated", "48912,no"]


def test_invalid_ark_is_reported_but_valid_naans_are_listed(tmp_path, capsys):
    source = tmp_path / "input.csv"
    write_input(source, ["ark:/41046/a", "not-an-ark"])

    exit_code = main([str(source)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out.strip() == "41046"
    assert "CSV row 3" in captured.err
