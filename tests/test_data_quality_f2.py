"""Fixtures F2 pequenas: unidade temporal, agregação completa e snapshot fixo."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

from quantlab.data_ingestion import build_identity, import_archive
from quantlab.data_quality import (
    EXPECTED_COLUMNS,
    audit_raw_objects,
    create_snapshot,
    epoch_value_to_utc,
    load_backtest_snapshot,
)

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _epoch(value: datetime, unit: str) -> int:
    delta = value - EPOCH
    micros = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    return micros // 1000 if unit == "ms" else micros


def _native_row(open_at: datetime, *, year: int) -> list[str]:
    unit = "ms" if year < 2025 else "us"
    step = 60_000 if unit == "ms" else 60_000_000
    price = "42000.00" if year < 2025 else "95000.00"
    return [
        str(_epoch(open_at, unit)),
        price,
        str(float(price) + 2),
        str(float(price) - 2),
        str(float(price) + 1),
        "0.01000000",
        str(_epoch(open_at, unit) + step - 1),
        "420.00000000",
        "3",
        "0.00400000",
        "168.00000000",
        "0",
    ]


def _make_month_zip(root: Path, year: int, *, missing: set[datetime] | None = None, header: bool) -> tuple[Path, Path]:
    identity = build_identity(year, 1)
    archive = root / identity.zip_name
    checksum = root / f"{identity.zip_name}.CHECKSUM"
    start = datetime(year, 1, 1, tzinfo=UTC)
    count = 120 if year == 2024 else 1
    if year == 2024 and missing is None:
        missing = {start + timedelta(minutes=90)}
    csv_text = io.StringIO(newline="")
    writer = csv.writer(csv_text, lineterminator="\n")
    if header:
        writer.writerow(EXPECTED_COLUMNS)
    for minute in range(count):
        opened = start + timedelta(minutes=minute)
        if missing and opened in missing:
            continue
        writer.writerow(_native_row(opened, year=year))
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        handle.writestr(identity.csv_name, csv_text.getvalue().encode("utf-8"))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum.write_text(f"{digest}  {identity.zip_name}\n", encoding="ascii")
    return archive, checksum


def _import_month(project: Path, source: Path, year: int) -> str:
    archive, checksum = _make_month_zip(source, year, header=(year == 2024))
    result = import_archive(archive, checksum, root=project)
    assert result.object_id is not None
    return result.object_id


class DataQualityF2Test(unittest.TestCase):
    def test_declared_units_and_header_preserve_first_data_row(self) -> None:
        same_instant = datetime(2024, 1, 1, tzinfo=UTC)
        self.assertEqual(
            epoch_value_to_utc(_epoch(same_instant, "ms"), "ms"),
            epoch_value_to_utc(_epoch(same_instant, "us"), "us"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, source = base / "project", base / "source"
            source.mkdir()
            raw_2024 = _import_month(project, source, 2024)
            raw_2025 = _import_month(project, source, 2025)
            reports = audit_raw_objects(project, raw_object_ids=(raw_2024, raw_2025))
            by_year = {report["period_start_utc"][:4]: report for report in reports}
            self.assertEqual(by_year["2024"]["timestamp_unit"], "ms")
            self.assertEqual(by_year["2025"]["timestamp_unit"], "us")
            self.assertEqual(by_year["2024"]["valid_rows"], 119)
            self.assertEqual(by_year["2025"]["valid_rows"], 1)
            self.assertTrue(by_year["2024"]["header_present"])
            self.assertFalse(by_year["2025"]["header_present"])
            self.assertEqual(by_year["2025"]["first_open_at_utc"], "2025-01-01T00:00:00.000000Z")

    def test_hourly_aggregate_requires_all_sixty_pass_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, source = base / "project", base / "source"
            source.mkdir()
            raw_id = _import_month(project, source, 2024)
            report = audit_raw_objects(project, raw_object_ids=(raw_id,))[0]
            one_hour = next(part for part in report["partitions"] if part["timeframe"] == "1h")
            one_minute = next(part for part in report["partitions"] if part["timeframe"] == "1m")
            self.assertEqual(one_minute["row_count"], 119)
            self.assertEqual(one_hour["row_count"], 1)
            self.assertEqual(one_hour["expected_count"], 744)
            self.assertEqual(one_hour["gap_count"], 743)
            blocked = [item for item in report["incomplete_windows"] if item["timeframe"] == "1h"]
            self.assertEqual(len(blocked), 743)
            blocked_gap = next(item for item in blocked if item["window_open_at_utc"] == "2024-01-01T01:00:00.000000Z")
            self.assertEqual(blocked_gap["observed_count"], 59)
            self.assertEqual(blocked_gap["status"], "INCOMPLETE_BLOCKED")
            self.assertEqual(report["aggregate_windows"]["1h"], {"complete": 1, "expected": 744, "incomplete": 743})
            self.assertTrue(any(
                item["start_at_utc"] == "2024-01-01T01:30:00.000000Z"
                and item["end_at_utc_exclusive"] == "2024-01-01T01:31:00.000000Z"
                and item["missing_minutes"] == 1
                for item in report["missing_1m_ranges"]
            ))
            minute_file = (project / one_minute["parquet_path"]).as_posix()
            hour_file = (project / one_hour["parquet_path"]).as_posix()
            database = duckdb.connect(":memory:")
            source = database.execute(
                f"SELECT count(*), arg_min(open, open_at_utc), max(high), min(low), "
                f"arg_max(close, open_at_utc), sum(volume_base), sum(trade_count) "
                f"FROM read_parquet('{minute_file}') WHERE open_at_utc >= TIMESTAMP '2024-01-01 00:00:00' "
                f"AND open_at_utc < TIMESTAMP '2024-01-01 01:00:00'"
            ).fetchone()
            aggregate = database.execute(
                f"SELECT open, high, low, close, volume_base, trade_count FROM read_parquet('{hour_file}') "
                f"WHERE open_at_utc = TIMESTAMP '2024-01-01 00:00:00'"
            ).fetchone()
            database.close()
            self.assertEqual(source[0], 60)
            self.assertEqual(source[1:], aggregate)

    def test_snapshot_lists_exact_partitions_and_keeps_raw_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, source = base / "project", base / "source"
            source.mkdir()
            raw_2024 = _import_month(project, source, 2024)
            report_2024 = audit_raw_objects(project, raw_object_ids=(raw_2024,))[0]
            one_minute = next(part for part in report_2024["partitions"] if part["timeframe"] == "1m")
            raw_path = project / "data" / "raw" / "binance" / "spot" / "monthly" / "klines" / "BTCUSDT" / "1m" / "2024-01" / raw_2024.rsplit("-", 1)[-1] / "BTCUSDT-1m-2024-01.zip"
            raw_digest = hashlib.sha256(raw_path.read_bytes()).hexdigest()
            snapshot = create_snapshot(project, [one_minute["partition_id"]], data_origin="SYNTHETIC")
            manifest_path = project / snapshot["manifest_path"]
            manifest_before = json.loads(manifest_path.read_text(encoding="utf-8"))

            raw_2025 = _import_month(project, source, 2025)
            audit_raw_objects(project, raw_object_ids=(raw_2025,))
            loaded = load_backtest_snapshot(project, snapshot["snapshot_id"])

            self.assertEqual(loaded.qa_status, "PASS")
            self.assertEqual(loaded.data_origin, "SYNTHETIC")
            self.assertEqual(len(loaded.candles_1m), 119)
            self.assertEqual(hashlib.sha256(raw_path.read_bytes()).hexdigest(), raw_digest)
            manifest_after = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest_before, manifest_after)
            self.assertEqual(manifest_after["sources"][0]["partition_id"], one_minute["partition_id"])
            self.assertEqual([snapshot["partition_ids"][0]], [manifest_after["sources"][0]["partition_id"]])


if __name__ == "__main__":
    unittest.main()
