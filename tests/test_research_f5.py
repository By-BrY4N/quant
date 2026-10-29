"""Verificação focal F5 de partição temporal e holdout de uso único."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from quantlab.backtest import BacktestConfig
from quantlab.data_ingestion import build_daily_identity, build_identity, import_archive
from quantlab.data_quality import (
    EXPECTED_COLUMNS,
    audit_raw_objects,
    create_snapshot,
    load_backtest_snapshot,
)
from quantlab.domain import StrategyName
from quantlab.research import (
    ResearchError,
    bind_holdout_snapshot,
    build_walk_forward_folds,
    create_experiment,
    ensure_snapshot_available,
    freeze_experiment,
    open_holdout,
    record_trial,
)

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRICE = Decimal("42000.00")


def _native_row(open_at: datetime) -> list[str]:
    millis = int((open_at - EPOCH).total_seconds() * 1000)
    return [
        str(millis), str(PRICE), str(PRICE + 2), str(PRICE - 2), str(PRICE + 1),
        "0.01000000", str(millis + 59_999), "420.00000000", "3",
        "0.00400000", "168.00000000", "0",
    ]


def _write_archive(source: Path, filename: str, csv_name: str, start: datetime, count: int) -> tuple[Path, Path]:
    archive = source / filename
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(EXPECTED_COLUMNS)
    for index in range(count):
        writer.writerow(_native_row(start + timedelta(minutes=index)))
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        handle.writestr(csv_name, output.getvalue().encode("utf-8"))
    checksum = source / f"{filename}.CHECKSUM"
    checksum.write_text(
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {filename}\n",
        encoding="ascii",
    )
    return archive, checksum


class ResearchF5Test(unittest.TestCase):
    def test_chronological_fold_and_holdout_open_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, source = base / "project", base / "source"
            source.mkdir()
            midnight = datetime(2024, 1, 1, tzinfo=UTC)

            monthly = build_identity(2024, 1)
            monthly_zip, monthly_checksum = _write_archive(
                source, monthly.zip_name, monthly.csv_name, midnight, 180,
            )
            dev_raw = import_archive(monthly_zip, monthly_checksum, root=project)
            dev_report = audit_raw_objects(project, raw_object_ids=(dev_raw.object_id,))[0]
            dev_partition = next(item for item in dev_report["partitions"] if item["timeframe"] == "1m")
            development = create_snapshot(
                project, [dev_partition["partition_id"]], data_origin="SYNTHETIC",
            )

            daily = build_daily_identity("2024-01-01")
            daily_zip, daily_checksum = _write_archive(
                source, daily.zip_name, daily.csv_name, midnight, 300,
            )
            holdout_raw = import_archive(daily_zip, daily_checksum, root=project)
            holdout_report = audit_raw_objects(project, raw_object_ids=(holdout_raw.object_id,))[0]
            holdout_partition = next(item for item in holdout_report["partitions"] if item["timeframe"] == "1m")
            holdout = create_snapshot(
                project, [holdout_partition["partition_id"]], data_origin="SYNTHETIC",
            )
            holdout_manifest = project / holdout["manifest_path"]
            manifest_content = json.loads(holdout_manifest.read_text(encoding="utf-8"))
            self.assertTrue(manifest_content["excluded_overlapping_partitions"])

            config = BacktestConfig(
                strategy=StrategyName.SMA_CROSS,
                decision_timeframe="1h",
                initial_cash_usdt=Decimal("1000"),
                fee_bps_per_side=Decimal("1"),
                slippage_bps_per_side=Decimal("1"),
                assumed_data_delay_seconds=Decimal("1"),
                sma_fast=1,
                sma_slow=2,
            )
            experiment = create_experiment(
                project,
                family_id="f5-temporal-check",
                version=1,
                development_snapshot_id=development["snapshot_id"],
                holdout_start_at_utc="2024-01-01T04:00:00Z",
                holdout_end_at_utc="2024-01-01T05:00:00Z",
                config=config,
                max_attempts=1,
                train_bars=2,
                test_bars=1,
                max_folds=1,
                minimum_test_folds=1,
                primary_metric="max_drawdown_pct",
                primary_direction="minimize",
                primary_threshold=Decimal("100"),
                allowed_parameter_values={},
                label_horizon=6,
                purge_bars=2,
            )
            self.assertEqual(experiment["spec"]["walk_forward"]["label_horizon"], 6)
            self.assertEqual(experiment["spec"]["walk_forward"]["purge_bars"], 2)
            binding = bind_holdout_snapshot(project, experiment["experiment_id"], holdout["snapshot_id"])
            self.assertEqual(binding["status"], "SEALED")
            with self.assertRaises(ResearchError):
                ensure_snapshot_available(project, holdout["snapshot_id"])
            trial = record_trial(project, experiment["experiment_id"], config)
            folds, exclusions = build_walk_forward_folds(
                load_backtest_snapshot(project, development["snapshot_id"]),
                decision_timeframe="1h",
                train_bars=2,
                test_bars=1,
                max_folds=1,
            )
            self.assertEqual(len(folds), 1)
            self.assertEqual(folds[0]["train_end_at_utc_exclusive"], folds[0]["test_start_at_utc"])
            self.assertFalse(folds[0]["parameters_fit"])
            self.assertFalse(exclusions)
            self.assertEqual(len(trial["folds"]), 1)
            self.assertIn("2x", trial["folds"][0]["cost_stress"])
            self.assertIn("3x", trial["folds"][0]["cost_stress"])

            freeze_experiment(project, experiment["experiment_id"], trial["trial_id"])
            with self.assertRaises(ResearchError):
                record_trial(project, experiment["experiment_id"], config)
            result = open_holdout(project, experiment["experiment_id"])
            self.assertEqual(result["status"], "OPENED_ONCE")
            self.assertFalse(result["post_open_edits_allowed"])
            self.assertIn("net_return_pct", result["buy_hold_metrics"])
            self.assertIn("2x", result["cost_stress"])
            self.assertIn("3x", result["cost_stress"])
            with self.assertRaises(ResearchError):
                open_holdout(project, experiment["experiment_id"])
            with self.assertRaises(ResearchError):
                ensure_snapshot_available(project, holdout["snapshot_id"])


if __name__ == "__main__":
    unittest.main()
