"""Read-only adapters for local QuantLab catalog and run artifacts."""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from quantlab.backtest import closed_trade_pnl_by_exit_timestamp
from quantlab.domain import Side, SimulatedFill


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _safe_child(root: Path, relative_path: str) -> Path | None:
    candidate = (root / relative_path).resolve()
    if not candidate.is_relative_to(root.resolve()):
        return None
    return candidate


def load_catalog_overview(root: Path) -> dict[str, Any]:
    """Read the existing catalog without creating tables or a database file."""
    empty: dict[str, Any] = {
        "raw_objects": [],
        "partitions": [],
        "snapshots": [],
        "sealed_holdout_ids": [],
        "experiments": [],
        "ingestion_errors": [],
        "catalog_error": None,
    }
    database = root / "state" / "quantlab.sqlite"
    if not database.is_file():
        return empty

    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=3)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "raw_objects" in tables:
                raw_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(raw_objects)")
                }
                granularity_column = "granularity" if "granularity" in raw_columns else "'monthly' AS granularity"
                day_column = "day" if "day" in raw_columns else "NULL AS day"
                empty["raw_objects"] = [
                    dict(row)
                    for row in connection.execute(
                        f"""SELECT object_id, symbol, interval, year, month, status,
                                  checksum_status, byte_size, sha256, source_url,
                                  acquisition_method, {granularity_column}, {day_column}, period_start_utc,
                                  period_end_utc, created_at_utc, raw_path, manifest_path
                           FROM raw_objects ORDER BY created_at_utc DESC, object_id DESC"""
                    )
                ]
            if "curated_partitions" in tables:
                empty["partitions"] = [
                    dict(row)
                    for row in connection.execute(
                        """SELECT partition_id, raw_object_id, symbol, timeframe, year, month,
                                  qa_status, row_count, expected_count, gap_count, coverage_pct,
                                  first_open_at_utc, last_open_at_utc, parquet_path,
                                  qa_json_path, qa_html_path, created_at_utc
                           FROM curated_partitions ORDER BY first_open_at_utc, timeframe, partition_id"""
                    )
                ]
            if "dataset_snapshots" in tables:
                snapshots = [
                    dict(row)
                    for row in connection.execute(
                        """SELECT snapshot_id, symbol, data_origin, qa_status, delay_assumed_ms,
                                  content_sha256, source_partition_count, manifest_path,
                                  qa_json_path, qa_html_path, created_at_utc
                           FROM dataset_snapshots ORDER BY created_at_utc DESC, snapshot_id DESC"""
                    )
                ]
                for snapshot in snapshots:
                    manifest_path = _safe_child(root, snapshot["manifest_path"])
                    snapshot["manifest"] = _read_json(manifest_path) if manifest_path else None
                empty["snapshots"] = snapshots
            if "research_experiments" in tables:
                empty["experiments"] = [
                    dict(row)
                    for row in connection.execute(
                        """SELECT experiment_id,family_id,version,status,development_snapshot_id,
                                  holdout_snapshot_id,frozen_trial_id,holdout_run_id,spec_sha256,
                                  created_at_utc,updated_at_utc
                           FROM research_experiments ORDER BY created_at_utc DESC"""
                    )
                ]
                empty["sealed_holdout_ids"] = sorted(
                    {item["holdout_snapshot_id"] for item in empty["experiments"] if item["holdout_snapshot_id"]}
                )
                sealed = set(empty["sealed_holdout_ids"])
                for snapshot in empty["snapshots"]:
                    snapshot["holdout_seal"] = "SELADO" if snapshot["snapshot_id"] in sealed else None
            if "ingestion_errors" in tables:
                empty["ingestion_errors"] = [
                    dict(row)
                    for row in connection.execute(
                        """SELECT identity, error_code, status_code, attempt, retry_count,
                                  message, created_at_utc
                           FROM ingestion_errors ORDER BY created_at_utc DESC LIMIT 30"""
                    )
                ]
        finally:
            connection.close()
    except (OSError, sqlite3.Error, json.JSONDecodeError) as error:
        empty["catalog_error"] = str(error)
    return empty


def list_runs(root: Path) -> list[dict[str, Any]]:
    """Find finalized F0/F3 runs by their manifest; ignore staging directories."""
    artifacts = root / "artifacts"
    candidates: list[Path] = []
    if artifacts.is_dir():
        candidates.extend(
            directory
            for directory in artifacts.iterdir()
            if directory.is_dir() and not directory.is_symlink() and (directory / "run.json").is_file()
        )
        backtests = artifacts / "backtests"
        if backtests.is_dir() and not backtests.is_symlink():
            candidates.extend(
                directory
                for directory in backtests.iterdir()
                if directory.is_dir() and not directory.is_symlink() and (directory / "run.json").is_file()
            )

    found: list[dict[str, Any]] = []
    root_resolved = root.resolve()
    for directory in candidates:
        try:
            if not directory.resolve().is_relative_to(root_resolved):
                continue
            manifest = _read_json(directory / "run.json")
            if not manifest or manifest.get("run_id") != directory.name:
                continue
            origin = manifest.get("data_origin", "UNKNOWN")
            if origin not in {"REAL", "SYNTHETIC"}:
                origin = "UNKNOWN"
            found.append(
                {
                    "run_id": directory.name,
                    "phase": manifest.get("phase", "UNKNOWN"),
                    "evidence_level": manifest.get("evidence_level", "UNKNOWN"),
                    "data_origin": origin,
                    "created_at_utc": manifest.get("created_at_utc", ""),
                    "directory": directory.resolve(),
                    "manifest": manifest,
                }
            )
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(found, key=lambda run: (str(run["created_at_utc"]), run["run_id"]), reverse=True)


def _as_utc(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp.astimezone(UTC)


def _simulated_fill(row: dict[str, str]) -> SimulatedFill:
    price = row.get("execution_price_usdt") or row.get("price_usdt") or "0"
    reference = row.get("reference_open_usdt") or row.get("reference_price_usdt") or price
    return SimulatedFill(
        timestamp_utc=_as_utc(row["timestamp_utc"]),
        side=Side(row["side"]),
        quantity_base=Decimal(row["quantity_base"]),
        price_usdt=Decimal(price),
        quote_amount_usdt=Decimal(row["quote_amount_usdt"]),
        fee_usdt=Decimal(row["fee_usdt"]),
        cash_after_usdt=Decimal(row["cash_after_usdt"]),
        base_qty_after=Decimal(row["base_qty_after"]),
        reference_price_usdt=Decimal(reference),
        slippage_cost_usdt=Decimal(row.get("slippage_cost_usdt") or "0"),
        fee_currency=row.get("fee_currency") or "USDT",
    )


def _operations(
    signals: list[dict[str, str]], fills: list[dict[str, str]], data_origin: str
) -> list[dict[str, Any]]:
    fills_by_key: dict[tuple[str, str], dict[str, str]] = {}
    realized_by_key: dict[tuple[str, str], str] = {}
    grouped_fills: dict[str, list[dict[str, str]]] = {}
    for fill in fills:
        strategy = fill.get("strategy", "UNKNOWN")
        timestamp = fill.get("timestamp_utc", "")
        fills_by_key[(strategy, timestamp)] = fill
        grouped_fills.setdefault(strategy, []).append(fill)
    for strategy, strategy_fills in grouped_fills.items():
        simulated = [_simulated_fill(row) for row in strategy_fills]
        realized = closed_trade_pnl_by_exit_timestamp(simulated)
        for timestamp, pnl in realized.items():
            realized_by_key[(strategy, timestamp.isoformat().replace("+00:00", "Z"))] = format(pnl, "f")

    def operation(signal: dict[str, str] | None, fill: dict[str, str] | None, index: int) -> dict[str, Any]:
        signal = signal or {}
        fill = fill or {}
        strategy = signal.get("strategy") or fill.get("strategy", "UNKNOWN")
        execution_at = signal.get("execution_at_utc", "")
        fill_at = fill.get("timestamp_utc", "") or execution_at
        status = signal.get("execution_status") or ("FILLED" if fill else "SEM FILL")
        return {
            "operation_id": f"{strategy}-{index + 1}",
            "strategy": strategy,
            "signal_at_utc": signal.get("generated_at_utc", ""),
            "available_at_utc": signal.get("available_at_utc", ""),
            "fill_at_utc": fill_at,
            "side": fill.get("side", ""),
            "quantity_base": fill.get("quantity_base", ""),
            "reference_price_usdt": fill.get("reference_open_usdt") or fill.get("reference_price_usdt", ""),
            "execution_price_usdt": fill.get("execution_price_usdt") or fill.get("price_usdt", ""),
            "fee_usdt": fill.get("fee_usdt", ""),
            "slippage_cost_usdt": fill.get("slippage_cost_usdt", ""),
            "realized_pnl_usdt": fill.get("realized_pnl_usdt") or realized_by_key.get((strategy, fill_at), ""),
            "execution_status": status,
            "veto_reason": signal.get("veto_reason", ""),
            "reason": signal.get("reason", "Registro de fill; este run não possui CSV de sinais." if fill else ""),
            "data_origin": data_origin,
        }

    if not signals:
        return [operation(None, fill, index) for index, fill in enumerate(fills)]

    result: list[dict[str, Any]] = []
    matched: set[tuple[str, str]] = set()
    for index, signal in enumerate(signals):
        strategy = signal.get("strategy", "UNKNOWN")
        execution_at = signal.get("execution_at_utc", "")
        key = (strategy, execution_at)
        fill = fills_by_key.get(key) if execution_at else None
        if fill is not None:
            matched.add(key)
        result.append(operation(signal, fill, index))

    for index, fill in enumerate(fills):
        key = (fill.get("strategy", "UNKNOWN"), fill.get("timestamp_utc", ""))
        if key not in matched:
            result.append(operation(None, fill, len(result) + index))
    return result


def load_run(root: Path, run_id: str) -> dict[str, Any]:
    """Load only an artifact directory previously found from a valid run manifest."""
    summary = next((item for item in list_runs(root) if item["run_id"] == run_id), None)
    if summary is None:
        raise ValueError(f"RUN_ID não encontrado ou manifesto inválido: {run_id}")
    directory: Path = summary["directory"]
    metrics = _read_json(directory / "metrics.json") or {}
    signals = _read_csv(directory / "signals.csv")
    fills = _read_csv(directory / "fills.csv")
    equity = _read_csv(directory / "equity.csv")
    sources = _read_json(directory / "sources.json")
    if sources is None:
        dataset = summary["manifest"].get("dataset", {})
        sources = {"data_origin": summary["data_origin"], "dataset": dataset}
    return {
        **summary,
        "metrics": metrics,
        "signals": signals,
        "fills": fills,
        "equity": equity,
        "operations": _operations(signals, fills, summary["data_origin"]),
        "sources": sources,
        "report_html": (directory / "report.html").read_text(encoding="utf-8")
        if (directory / "report.html").is_file()
        else None,
        "run_json_text": (directory / "run.json").read_text(encoding="utf-8"),
        "metrics_json_text": (directory / "metrics.json").read_text(encoding="utf-8")
        if (directory / "metrics.json").is_file()
        else "{}\n",
    }


def available_strategies(run: dict[str, Any]) -> list[str]:
    metrics = run.get("metrics", {}).get("strategies", {})
    names = set(metrics) if isinstance(metrics, dict) else set()
    names.update(row.get("strategy", "") for row in run.get("signals", []))
    names.update(row.get("strategy", "") for row in run.get("fills", []))
    return sorted(name for name in names if name)
