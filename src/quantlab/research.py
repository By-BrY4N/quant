"""F5: folds temporais, contratos de experimento e holdout selado."""

from __future__ import annotations

import hashlib
import html
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any, Iterator, Mapping
from uuid import uuid4

from quantlab.backtest import (
    BacktestConfig,
    BacktestResult,
    BacktestSnapshot,
    StrategyResult,
    TIMEFRAME_MINUTES,
    _aggregate_decision_candles,
    run_backtest,
    run_backtest_window,
)
from quantlab.backtest_artifacts import _config_dict, write_backtest_artifacts
from quantlab.domain import StrategyName
from quantlab.data_quality import load_backtest_snapshot

METRIC_DIRECTIONS = {
    "net_return_pct": "maximize",
    "max_drawdown_pct": "minimize",
    "exposure_pct": "minimize",
    "turnover_quote_usdt": "minimize",
    "daily_sharpe": "maximize",
}


class ResearchError(ValueError):
    """Falha de contrato científico que pode ser exibida na UI/CLI."""


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@contextmanager
def _db(root: Path) -> Iterator[sqlite3.Connection]:
    path = root / "state" / "quantlab.sqlite"
    if not path.is_file():
        raise ResearchError("O catálogo F2 ainda não existe; crie um snapshot antes do experimento.")
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS research_experiments (
            experiment_id TEXT PRIMARY KEY,
            family_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('OPEN','FROZEN','HOLDOUT_OPENED')),
            spec_json TEXT NOT NULL,
            spec_sha256 TEXT NOT NULL,
            development_snapshot_id TEXT NOT NULL,
            holdout_snapshot_id TEXT,
            frozen_trial_id TEXT,
            holdout_run_id TEXT,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            UNIQUE(family_id, version)
        );
        CREATE TABLE IF NOT EXISTS research_trials (
            trial_id TEXT PRIMARY KEY,
            experiment_id TEXT NOT NULL REFERENCES research_experiments(experiment_id),
            trial_index INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('RUNNING','COMPLETE','FAILED')),
            backtest_run_id TEXT,
            config_json TEXT NOT NULL,
            config_sha256 TEXT NOT NULL,
            trial_manifest_path TEXT,
            candidate_status TEXT,
            created_at_utc TEXT NOT NULL,
            finished_at_utc TEXT,
            UNIQUE(experiment_id, trial_index)
        );
        CREATE TABLE IF NOT EXISTS research_events (
            event_id TEXT PRIMARY KEY,
            experiment_id TEXT NOT NULL REFERENCES research_experiments(experiment_id),
            event_type TEXT NOT NULL,
            occurred_at_utc TEXT NOT NULL,
            details_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS research_trials_by_experiment
            ON research_trials(experiment_id, trial_index);
        """
    )
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ResearchError("Datas de experimento precisam conter timezone explícito.")
    return parsed.astimezone(UTC)


def _snapshot_extent(root: Path, snapshot_id: str) -> dict[str, Any]:
    with _db(root) as connection:
        snapshot = connection.execute(
            "SELECT snapshot_id, symbol, data_origin, qa_status, content_sha256 FROM dataset_snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if snapshot is None or snapshot["qa_status"] != "PASS":
            raise ResearchError(f"Snapshot inexistente ou sem QA PASS: {snapshot_id}.")
        rows = connection.execute(
            """SELECT p.first_open_at_utc, p.last_open_at_utc, p.row_count, p.expected_count,
                      p.gap_count, p.coverage_pct, p.partition_id
               FROM snapshot_sources s
               JOIN curated_partitions p USING(partition_id)
               WHERE s.snapshot_id=? AND p.timeframe='1m'
               ORDER BY p.first_open_at_utc, p.partition_id""",
            (snapshot_id,),
        ).fetchall()
    if not rows or any(not row["first_open_at_utc"] or not row["last_open_at_utc"] for row in rows):
        raise ResearchError(f"Snapshot não tem cobertura temporal 1m utilizável: {snapshot_id}.")
    first = _utc(rows[0]["first_open_at_utc"])
    last = _utc(rows[-1]["last_open_at_utc"]) + timedelta(minutes=1)
    return {
        **dict(snapshot),
        "start_at_utc": first.isoformat().replace("+00:00", "Z"),
        "end_at_utc_exclusive": last.isoformat().replace("+00:00", "Z"),
        "row_count": sum(int(row["row_count"]) for row in rows),
        "expected_count": sum(int(row["expected_count"]) for row in rows),
        "gap_count": sum(int(row["gap_count"]) for row in rows),
        "coverage_pct": min(float(row["coverage_pct"]) for row in rows),
        "partition_ids": [row["partition_id"] for row in rows],
    }


def _assert_snapshot_unseen(root: Path, snapshot_id: str) -> None:
    artifacts = root / "artifacts"
    if not artifacts.is_dir():
        return
    for manifest_path in artifacts.rglob("run.json"):
        if manifest_path.is_symlink():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        snapshot = manifest.get("snapshot") or {}
        if snapshot.get("snapshot_id") == snapshot_id:
            raise ResearchError(
                f"Snapshot {snapshot_id} já aparece em um run salvo; ele não pode ser lacrado como holdout novo."
            )


def _event(connection: sqlite3.Connection, experiment_id: str, event_type: str, details: Mapping[str, Any]) -> str:
    event_id = f"research-event-{uuid4().hex}"
    connection.execute(
        "INSERT INTO research_events VALUES (?, ?, ?, ?, ?)",
        (event_id, experiment_id, event_type, _now(), _canonical(dict(details))),
    )
    return event_id


def _config_json(config: BacktestConfig) -> dict[str, Any]:
    return _config_dict(
        # Only this helper's config projection is used; it matches the F3 run hash contract.
        type("_Result", (), {"config": config})()
    )


def _as_config(data: Mapping[str, Any]) -> BacktestConfig:
    return BacktestConfig(
        strategy=StrategyName(str(data["strategy"]).upper()),
        decision_timeframe=str(data["decision_timeframe"]),
        initial_cash_usdt=Decimal(str(data["initial_cash_usdt"])),
        fee_bps_per_side=Decimal(str(data["fee_bps_per_side"])),
        slippage_bps_per_side=Decimal(str(data["slippage_bps_per_side"])),
        assumed_data_delay_seconds=Decimal(str(data["assumed_data_delay_seconds"])),
        sma_fast=int(data["sma_fast"]),
        sma_slow=int(data["sma_slow"]),
        breakout_lookback=int(data["breakout_lookback"]),
        momentum_lookback=int(data.get("momentum_lookback", 24)),
        mean_reversion_lookback=int(data.get("mean_reversion_lookback", 30)),
        mean_reversion_entry_z=Decimal(str(data.get("mean_reversion_entry_z", "-2"))),
        mean_reversion_exit_z=Decimal(str(data.get("mean_reversion_exit_z", "0"))),
    )


def _run_metrics(result: StrategyResult) -> dict[str, str | int | bool]:
    metrics = result.metrics
    return {
        "net_return_pct": str(metrics.net_return_pct),
        "max_drawdown_pct": str(metrics.max_drawdown_pct),
        "exposure_pct": str(metrics.exposure_pct),
        "turnover_quote_usdt": str(metrics.turnover_quote_usdt),
        "net_pnl_usdt": str(metrics.net_pnl_usdt),
        "total_fees_usdt": str(metrics.total_fees_usdt),
        "total_slippage_cost_usdt": str(metrics.total_slippage_cost_usdt),
        "final_equity_usdt": str(metrics.final_equity_usdt),
        "daily_sharpe": str(metrics.daily_sharpe) if metrics.daily_sharpe is not None else "NaN",
        "daily_sharpe_observation_count": metrics.daily_sharpe_observation_count,
        "daily_sharpe_annualization_days": metrics.daily_sharpe_annualization_days,
        "daily_sharpe_risk_free_daily_return": str(metrics.daily_sharpe_risk_free_daily_return),
        "daily_sharpe_return_frequency": metrics.daily_sharpe_return_frequency,
        "win_rate_pct": str(metrics.win_rate_pct) if metrics.win_rate_pct is not None else "NaN",
        "profit_factor": str(metrics.profit_factor) if metrics.profit_factor is not None else "NaN",
        "closed_trade_count": metrics.closed_trade_count,
        "buy_count": metrics.buy_count,
        "sell_count": metrics.sell_count,
        "veto_count": metrics.veto_count,
        "open_position": metrics.open_position,
    }


def _contiguous_segments(snapshot: BacktestSnapshot, timeframe: str) -> tuple[list[list[Any]], int]:
    bars, discarded = _aggregate_decision_candles(snapshot.candles_1m, timeframe)
    segments: list[list[Any]] = []
    current: list[Any] = []
    for bar in bars:
        if current and bar.open_at_utc != current[-1].end_at_utc:
            segments.append(current)
            current = []
        current.append(bar)
    if current:
        segments.append(current)
    return segments, discarded


def build_walk_forward_folds(
    snapshot: BacktestSnapshot,
    *,
    decision_timeframe: str,
    train_bars: int,
    test_bars: int,
    max_folds: int = 5,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if train_bars < 2 or test_bars < 1 or not 1 <= max_folds <= 5:
        raise ResearchError("Folds exigem treino >= 2 barras, teste >= 1 e no máximo 5 folds.")
    segments, discarded_windows = _contiguous_segments(snapshot, decision_timeframe)
    folds: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    if discarded_windows:
        exclusions.append({
            "reason": "INCOMPLETE_DECISION_WINDOWS_BLOCKED",
            "timeframe": decision_timeframe,
            "window_count": discarded_windows,
        })
    for left, right in zip(segments, segments[1:]):
        exclusions.append({
            "reason": "GAP_SPLITS_TEMPORAL_SEGMENTS",
            "previous_segment_end_at_utc_exclusive": left[-1].end_at_utc.isoformat().replace("+00:00", "Z"),
            "next_segment_start_at_utc": right[0].open_at_utc.isoformat().replace("+00:00", "Z"),
        })
    for segment_index, segment in enumerate(segments, start=1):
        if len(segment) < train_bars + test_bars:
            exclusions.append({
                "segment": segment_index,
                "start_at_utc": segment[0].open_at_utc.isoformat().replace("+00:00", "Z"),
                "end_at_utc_exclusive": segment[-1].end_at_utc.isoformat().replace("+00:00", "Z"),
                "available_decision_bars": len(segment),
                "required_decision_bars": train_bars + test_bars,
                "reason": "SEGMENT_TOO_SHORT_FOR_TRAIN_AND_TEST",
            })
            continue
        train_end_index = train_bars
        while train_end_index + test_bars <= len(segment) and len(folds) < max_folds:
            test_start_index = train_end_index
            test_end_index = test_start_index + test_bars
            folds.append({
                "fold": len(folds) + 1,
                "segment": segment_index,
                "train_start_at_utc": segment[0].open_at_utc.isoformat().replace("+00:00", "Z"),
                "train_end_at_utc_exclusive": segment[train_end_index - 1].end_at_utc.isoformat().replace("+00:00", "Z"),
                "test_start_at_utc": segment[test_start_index].open_at_utc.isoformat().replace("+00:00", "Z"),
                "test_end_at_utc_exclusive": segment[test_end_index - 1].end_at_utc.isoformat().replace("+00:00", "Z"),
                "train_decision_bars": train_end_index,
                "test_decision_bars": test_bars,
                "parameters_fit": False,
                "note": "Regra F3 determinística; parâmetros fixos neste trial, sem ajuste automático no fold.",
            })
            train_end_index += test_bars
        if len(folds) >= max_folds:
            break
    if len(folds) < max_folds and segments:
        exclusions.append({
            "reason": "MAX_AVAILABLE_FOLDS_REACHED",
            "requested_max_folds": max_folds,
            "available_folds": len(folds),
        })
    return folds, exclusions


def _fold_range_metrics(result: BacktestResult, start: datetime, end: datetime) -> dict[str, Any]:
    by_strategy = {item.strategy: item for item in result.results}
    selected = by_strategy[result.config.strategy]
    benchmark = by_strategy[StrategyName.BUY_HOLD]
    return {
        "strategy": _run_metrics(selected),
        "buy_hold": _run_metrics(benchmark),
        "gap_count": result.audit.get("gap_count", 0),
        "missing_1m_minutes": result.audit.get("missing_1m_minutes", 0),
        "evaluation_window": {
            "start_at_utc": start.isoformat().replace("+00:00", "Z"),
            "end_at_utc_exclusive": end.isoformat().replace("+00:00", "Z"),
        },
    }


def _stress_result(
    snapshot: BacktestSnapshot,
    config: BacktestConfig,
    fold: Mapping[str, Any],
    multiplier: int,
    *,
    evidence_level: str = "WALK_FORWARD",
) -> dict[str, Any]:
    stressed = replace(
        config,
        fee_bps_per_side=config.fee_bps_per_side * multiplier,
        slippage_bps_per_side=config.slippage_bps_per_side * multiplier,
    )
    result = run_backtest_window(
        snapshot,
        stressed,
        start_at_utc=_utc(str(fold["test_start_at_utc"])),
        end_at_utc=_utc(str(fold["test_end_at_utc_exclusive"])),
        evidence_level=evidence_level,
    )
    selected = next(item for item in result.results if item.strategy == config.strategy)
    return {
        "multiplier": multiplier,
        "fee_bps_per_side": str(stressed.fee_bps_per_side),
        "slippage_bps_per_side": str(stressed.slippage_bps_per_side),
        "metrics": _run_metrics(selected),
    }


def _candidate_status(
    spec: Mapping[str, Any],
    fold_results: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    minimum = int(spec["minimum_test_folds"])
    metric = str(spec["primary_metric"])
    direction = str(spec["primary_direction"])
    values = [Decimal(str(item["test"]["strategy"].get(metric, "NaN"))) for item in fold_results]
    values = [value for value in values if value.is_finite()]
    if len(values) < minimum:
        return "INCONCLUSIVO", {
            "reason": "FOLDS_INSUFICIENTES",
            "available_test_folds": len(values),
            "minimum_test_folds": minimum,
            "primary_metric": metric,
            "primary_direction": direction,
            "threshold": str(spec["primary_threshold"]),
        }
    aggregate = Decimal(str(median(values)))
    threshold = Decimal(str(spec["primary_threshold"]))
    passed = aggregate >= threshold if direction == "maximize" else aggregate <= threshold
    return ("CANDIDATO_A_PAPER" if passed else "NO_GO"), {
        "primary_metric": metric,
        "primary_direction": direction,
        "threshold": str(threshold),
        "fold_values": [str(value) for value in values],
        "median_test_value": str(aggregate),
        "criterion_passed": passed,
        "interpretation": "critério de pesquisa pré-registrado; não é garantia de edge nem recomendação de capital real",
    }


def _write_trial_report(path: Path, manifest: Mapping[str, Any]) -> None:
    metrics = manifest["decision"]
    fold_rows = "".join(
        "<tr><td>{fold}</td><td>{start}</td><td>{end}</td><td>{metric}</td><td>{dd}</td><td>{sharpe}</td><td>{win_rate}</td><td>{profit_factor}</td><td>{stress}</td></tr>".format(
            fold=html.escape(str(item["fold"])),
            start=html.escape(str(item["test_start_at_utc"])),
            end=html.escape(str(item["test_end_at_utc_exclusive"])),
            metric=html.escape(str(item["test"]["strategy"].get(manifest["experiment"]["primary_metric"], ""))),
            dd=html.escape(str(item["test"]["strategy"].get("max_drawdown_pct", ""))),
            sharpe=html.escape(str(item["test"]["strategy"].get("daily_sharpe", "N/D"))),
            win_rate=html.escape(str(item["test"]["strategy"].get("win_rate_pct", "N/D"))),
            profit_factor=html.escape(str(item["test"]["strategy"].get("profit_factor", "N/D"))),
            stress=html.escape(str(item.get("cost_stress", {}).get("3x", {}).get("metrics", {}).get("net_return_pct", "n/a"))),
        )
        for item in manifest["folds"]
    )
    path.write_text(
        "<!doctype html><html lang='pt-BR'><meta charset='utf-8'><title>Trial F5</title>"
        "<style>body{font:15px system-ui;max-width:1100px;margin:30px auto;padding:0 16px;color:#1e293b}"
        "table{border-collapse:collapse;width:100%}td,th{padding:8px;border-bottom:1px solid #ddd;text-align:left}</style>"
        f"<h1>Trial {html.escape(str(manifest['trial_id']))}</h1>"
        f"<p>Estado: <strong>{html.escape(str(metrics['status']))}</strong> · config SHA-256 "
        f"<code>{html.escape(str(manifest['config_sha256']))}</code></p>"
        f"<p>Métrica pré-registrada: {html.escape(str(metrics.get('primary_metric','')))} "
        f"{html.escape(str(metrics.get('primary_direction','')))} {html.escape(str(metrics.get('threshold','')))}; "
        f"mediana OOS por folds: {html.escape(str(metrics.get('median_test_value','INCONCLUSIVO')))}.</p>"
        "<table><thead><tr><th>Fold</th><th>Teste inicial</th><th>Teste final</th><th>Métrica primária</th>"
        f"<th>Drawdown máx. %</th><th>Sharpe diário</th><th>Win rate %</th><th>Profit factor</th>"
        f"<th>Retorno com custo 3x %</th></tr></thead><tbody>{fold_rows}</tbody></table>"
        "<p>Dados anteriores ao teste servem apenas para warm-up causal. Folds são cronológicos; sem shuffle. "
        "A estratégia determinística não ajusta parâmetros automaticamente no fold.</p></html>",
        encoding="utf-8",
    )


def create_experiment(
    root: Path,
    *,
    family_id: str,
    version: int,
    development_snapshot_id: str,
    config: BacktestConfig,
    max_attempts: int,
    train_bars: int,
    test_bars: int,
    max_folds: int,
    primary_metric: str,
    primary_direction: str,
    primary_threshold: Decimal,
    minimum_test_folds: int,
    holdout_start_at_utc: str,
    holdout_end_at_utc: str,
    holdout_snapshot_id: str | None = None,
    allowed_parameter_values: Mapping[str, list[int]] | None = None,
    label_horizon: int | None = None,
    purge_bars: int = 0,
) -> dict[str, Any]:
    family_id = family_id.strip()
    if not family_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in family_id):
        raise ResearchError("family_id deve usar somente letras, números, ponto, sublinhado ou hífen.")
    if version < 1 or max_attempts < 1 or minimum_test_folds < 1:
        raise ResearchError("Versão, tentativas e folds mínimos devem ser positivos.")
    if max_attempts > 50:
        raise ResearchError("F5 limita o orçamento a 50 tentativas por família/versão.")
    if primary_metric not in METRIC_DIRECTIONS:
        raise ResearchError("Métrica primária não suportada.")
    if primary_direction != METRIC_DIRECTIONS[primary_metric]:
        raise ResearchError(f"A direção de {primary_metric} é {METRIC_DIRECTIONS[primary_metric]}.")
    if not primary_threshold.is_finite():
        raise ResearchError("Critério primário precisa ser finito.")
    if config.fee_bps_per_side * 3 >= Decimal("10000") or config.slippage_bps_per_side * 3 >= Decimal("10000"):
        raise ResearchError("Fee e slippage base precisam permitir o stress 3x abaixo de 10000 bps por lado.")
    holdout_start, holdout_end = _utc(holdout_start_at_utc), _utc(holdout_end_at_utc)
    if holdout_end <= holdout_start:
        raise ResearchError("Período do holdout inválido.")
    if train_bars < 2 or test_bars < 1 or not 1 <= max_folds <= 5:
        raise ResearchError("Folds exigem treino >= 2 barras, teste >= 1 e no máximo 5 folds.")
    if not 1 <= minimum_test_folds <= max_folds:
        raise ResearchError("Folds mínimos devem estar entre 1 e o máximo de folds registrado.")
    if (label_horizon is not None and label_horizon < 1) or purge_bars < 0:
        raise ResearchError("Horizon opcional deve ser positivo e purge não pode ser negativo.")
    dev_extent = _snapshot_extent(root, development_snapshot_id)
    ensure_snapshot_available(root, development_snapshot_id)
    if holdout_start < _utc(dev_extent["end_at_utc_exclusive"]):
        raise ResearchError(
            "O holdout deve começar depois do fim coberto pelo snapshot de desenvolvimento; "
            "períodos sobrepostos não formam uma avaliação final independente."
        )
    if holdout_snapshot_id:
        if holdout_snapshot_id == development_snapshot_id:
            raise ResearchError("Desenvolvimento e holdout precisam usar snapshots separados.")
        holdout_extent = _snapshot_extent(root, holdout_snapshot_id)
        if holdout_extent["symbol"] != dev_extent["symbol"]:
            raise ResearchError("O holdout precisa usar o mesmo instrumento do snapshot de desenvolvimento.")
        _validate_holdout_snapshot(root, holdout_snapshot_id, holdout_start, holdout_end, config)
        _assert_snapshot_unseen(root, holdout_snapshot_id)
    base_config = _config_json(config)
    allowed = dict(allowed_parameter_values or {})
    parameter_keys = {
        "sma_fast", "sma_slow", "breakout_lookback",
        "momentum_lookback", "mean_reversion_lookback",
    }
    if set(allowed) - parameter_keys:
        raise ResearchError("Somente parâmetros de estratégia F3 podem variar entre trials.")
    for key, values in allowed.items():
        if not values or any(not isinstance(value, int) or value < 1 for value in values):
            raise ResearchError(f"Lista de valores permitidos inválida para {key}.")
        if int(base_config[key]) not in values:
            raise ResearchError(f"O valor inicial de {key} também precisa estar no conjunto pré-registrado.")
    spec: dict[str, Any] = {
        "spec_version": 1,
        "family_id": family_id,
        "version": version,
        "development_snapshot_id": development_snapshot_id,
        "development_snapshot_sha256": dev_extent["content_sha256"],
        "development_instrument_id": f"BINANCE:SPOT:{dev_extent['symbol']}",
        "base_config": base_config,
        "allowed_parameter_values": allowed,
        "max_attempts": max_attempts,
        "walk_forward": {
            "train_bars": train_bars,
            "test_bars": test_bars,
            "max_folds": max_folds,
            "bar_unit": config.decision_timeframe,
            "shuffle": False,
            "label_horizon": label_horizon,
            "purge_bars": purge_bars,
            "note": "Regras determinísticas F3 sem labels futuros; horizon/purge são campos registrados para modelos futuros e não alteram folds nesta fase.",
        },
        "primary_metric": primary_metric,
        "primary_direction": primary_direction,
        "primary_threshold": str(primary_threshold),
        "minimum_test_folds": minimum_test_folds,
        "holdout_period": {
            "start_at_utc": holdout_start.isoformat().replace("+00:00", "Z"),
            "end_at_utc_exclusive": holdout_end.isoformat().replace("+00:00", "Z"),
        },
        "evaluation_policy": {
            "flat_start": True,
            "warmup_uses_earlier_snapshot_bars": True,
            "cost_stress_multipliers": [2, 3],
            "holdout_openings_allowed": 1,
            "holdout_selection_allowed": False,
        },
    }
    spec_sha = _sha(spec)
    experiment_id = f"{family_id}-v{version}"
    with _db(root) as connection:
        connection.execute("BEGIN IMMEDIATE")
        exists = connection.execute(
            "SELECT experiment_id FROM research_experiments WHERE family_id=? AND version=?",
            (family_id, version),
        ).fetchone()
        if exists:
            raise ResearchError(f"Experimento {experiment_id} já existe; versões registradas são imutáveis.")
        development_role = connection.execute(
            "SELECT experiment_id FROM research_experiments WHERE holdout_snapshot_id=? LIMIT 1",
            (development_snapshot_id,),
        ).fetchone()
        if development_role:
            raise ResearchError(
                f"Snapshot de desenvolvimento está selado como holdout da versão {development_role['experiment_id']}."
            )
        if holdout_snapshot_id:
            used = connection.execute(
                """SELECT experiment_id FROM research_experiments
                   WHERE holdout_snapshot_id=? OR development_snapshot_id=? LIMIT 1""",
                (holdout_snapshot_id, holdout_snapshot_id),
            ).fetchone()
            if used:
                raise ResearchError(
                    f"Snapshot {holdout_snapshot_id} já está reservado pela versão {used['experiment_id']} "
                    "como desenvolvimento ou holdout."
                )
        connection.execute(
            """INSERT INTO research_experiments
               (experiment_id,family_id,version,status,spec_json,spec_sha256,development_snapshot_id,
                holdout_snapshot_id,frozen_trial_id,holdout_run_id,created_at_utc,updated_at_utc)
               VALUES (?,?,?,'OPEN',?,?,?, ?,NULL,NULL,?,?)""",
            (experiment_id, family_id, version, _canonical(spec), spec_sha, development_snapshot_id,
             holdout_snapshot_id, _now(), _now()),
        )
        _event(connection, experiment_id, "EXPERIMENT_REGISTERED", {
            "spec_sha256": spec_sha,
            "holdout_snapshot_id": holdout_snapshot_id,
            "holdout_access": "SEALED",
            "holdout_period": spec["holdout_period"],
        })
    return {"experiment_id": experiment_id, "status": "OPEN", "spec": spec, "spec_sha256": spec_sha}


def _validate_holdout_snapshot(
    root: Path, snapshot_id: str, start: datetime, end: datetime, config: BacktestConfig
) -> None:
    snapshot = load_backtest_snapshot(root, snapshot_id)
    candles = snapshot.candles_1m
    evaluation = [bar for bar in candles if bar.open_at_utc >= start and bar.end_at_utc <= end]
    expected = int((end - start) / timedelta(minutes=1))
    if (
        len(evaluation) != expected
        or not evaluation
        or evaluation[0].open_at_utc != start
        or evaluation[-1].end_at_utc != end
        or any(left.end_at_utc != right.open_at_utc for left, right in zip(evaluation, evaluation[1:]))
    ):
        raise ResearchError(
            f"Holdout {snapshot_id} não cobre integralmente o período registrado; avaliação final continua selada."
        )
    width = TIMEFRAME_MINUTES[config.decision_timeframe]
    if (
        start.second or start.microsecond or end.second or end.microsecond
        or int(start.timestamp()) % (width * 60)
        or int(end.timestamp()) % (width * 60)
    ):
        raise ResearchError(f"Limites do holdout devem alinhar às barras de {config.decision_timeframe}.")
    warmup_needed = max(
        config.sma_slow if config.strategy == StrategyName.SMA_CROSS else 1,
        config.breakout_lookback + 1 if config.strategy == StrategyName.BREAKOUT else 1,
        config.momentum_lookback + 1 if config.strategy == StrategyName.MOMENTUM else 1,
        config.mean_reversion_lookback if config.strategy == StrategyName.MEAN_REVERSION else 1,
    )
    prior_bars, _ = _aggregate_decision_candles(candles, config.decision_timeframe)
    prior_bars = [bar for bar in prior_bars if bar.end_at_utc <= start]
    contiguous = 0
    for bar in reversed(prior_bars):
        if contiguous and bar.end_at_utc != prior_bars[-contiguous].open_at_utc:
            break
        contiguous += 1
    if contiguous < warmup_needed:
        raise ResearchError(
            f"Holdout precisa de {warmup_needed} barras causais de warm-up anteriores; encontrou {contiguous}."
        )


def bind_holdout_snapshot(root: Path, experiment_id: str, snapshot_id: str) -> dict[str, Any]:
    with _db(root) as connection:
        row = connection.execute(
            "SELECT * FROM research_experiments WHERE experiment_id=?",
            (experiment_id,),
        ).fetchone()
        if row is None:
            raise ResearchError(f"Experimento não encontrado: {experiment_id}.")
        if row["status"] != "OPEN" or row["holdout_snapshot_id"]:
            raise ResearchError("A versão já congelou ou já tem snapshot holdout vinculado.")
        spec = json.loads(row["spec_json"])
        if snapshot_id == row["development_snapshot_id"]:
            raise ResearchError("O snapshot de holdout precisa ser diferente do snapshot de desenvolvimento.")
        development_extent = _snapshot_extent(root, row["development_snapshot_id"])
        holdout_start = _utc(spec["holdout_period"]["start_at_utc"])
        holdout_end = _utc(spec["holdout_period"]["end_at_utc_exclusive"])
        if holdout_start < _utc(development_extent["end_at_utc_exclusive"]):
            raise ResearchError("O holdout deve começar depois do fim coberto pelo snapshot de desenvolvimento.")
        holdout_extent = _snapshot_extent(root, snapshot_id)
        if holdout_extent["symbol"] != development_extent["symbol"]:
            raise ResearchError("O holdout precisa usar o mesmo instrumento do snapshot de desenvolvimento.")
        config = _as_config(spec["base_config"])
        _validate_holdout_snapshot(
            root, snapshot_id, holdout_start, holdout_end, config,
        )
        _assert_snapshot_unseen(root, snapshot_id)
        used = connection.execute(
            """SELECT experiment_id FROM research_experiments
               WHERE holdout_snapshot_id=? OR development_snapshot_id=? LIMIT 1""",
            (snapshot_id, snapshot_id),
        ).fetchone()
        if used:
            raise ResearchError(f"Snapshot holdout já está reservado pela versão {used['experiment_id']}.")
        connection.execute("BEGIN IMMEDIATE")
        current = _load_experiment(connection, experiment_id)
        if current["status"] != "OPEN" or current["holdout_snapshot_id"]:
            raise ResearchError("A versão foi congelada ou recebeu holdout enquanto a vinculação estava em andamento.")
        role_conflict = connection.execute(
            """SELECT experiment_id FROM research_experiments
               WHERE experiment_id<>? AND (holdout_snapshot_id=? OR development_snapshot_id=?) LIMIT 1""",
            (experiment_id, snapshot_id, snapshot_id),
        ).fetchone()
        if role_conflict:
            raise ResearchError(f"Snapshot já está reservado pela versão {role_conflict['experiment_id']}.")
        connection.execute(
            "UPDATE research_experiments SET holdout_snapshot_id=?, updated_at_utc=? WHERE experiment_id=? AND status='OPEN' AND holdout_snapshot_id IS NULL",
            (snapshot_id, _now(), experiment_id),
        )
        _event(connection, experiment_id, "HOLDOUT_SNAPSHOT_BOUND", {
            "holdout_snapshot_id": snapshot_id,
            "content_sha256": holdout_extent["content_sha256"],
            "period": spec["holdout_period"],
            "run_previously_seen": False,
        })
    return {"experiment_id": experiment_id, "holdout_snapshot_id": snapshot_id, "status": "SEALED"}


def ensure_snapshot_available(root: Path, snapshot_id: str) -> None:
    with _db(root) as connection:
        row = connection.execute(
            """SELECT experiment_id, status FROM research_experiments
               WHERE holdout_snapshot_id=? LIMIT 1""",
            (snapshot_id,),
        ).fetchone()
    if row:
        raise ResearchError(
            f"Snapshot {snapshot_id} está SELADO como holdout da versão {row['experiment_id']}; "
            "use somente a avaliação formal após congelar a família."
        )


def _load_experiment(connection: sqlite3.Connection, experiment_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM research_experiments WHERE experiment_id=?",
        (experiment_id,),
    ).fetchone()
    if row is None:
        raise ResearchError(f"Experimento não encontrado: {experiment_id}.")
    return row


def _validate_trial_config(spec: Mapping[str, Any], config: BacktestConfig) -> dict[str, Any]:
    candidate = _config_json(config)
    base = spec["base_config"]
    for key, expected in base.items():
        if key in {
            "sma_fast", "sma_slow", "breakout_lookback",
            "momentum_lookback", "mean_reversion_lookback",
        } and key in spec["allowed_parameter_values"]:
            allowed = spec["allowed_parameter_values"][key]
            if candidate[key] not in allowed:
                raise ResearchError(f"Parâmetro {key}={candidate[key]} não está no orçamento pré-registrado {allowed}.")
        elif candidate[key] != expected:
            raise ResearchError(f"Trial alterou {key}; apenas parâmetros declarados em allowed_parameter_values podem variar.")
    return candidate


def record_trial(root: Path, experiment_id: str, config: BacktestConfig) -> dict[str, Any]:
    with _db(root) as connection:
        connection.execute("BEGIN IMMEDIATE")
        experiment = _load_experiment(connection, experiment_id)
        if experiment["status"] != "OPEN":
            raise ResearchError("Versão congelada: novos trials estão bloqueados.")
        spec = json.loads(experiment["spec_json"])
        config_payload = _validate_trial_config(spec, config)
        existing_count = int(connection.execute(
            "SELECT count(*) FROM research_trials WHERE experiment_id=?",
            (experiment_id,),
        ).fetchone()[0])
        if existing_count >= int(spec["max_attempts"]):
            raise ResearchError("Orçamento de tentativas pré-registrado foi esgotado.")
        role_conflict = connection.execute(
            """SELECT experiment_id FROM research_experiments
               WHERE holdout_snapshot_id=? AND experiment_id<>? LIMIT 1""",
            (experiment["development_snapshot_id"], experiment_id),
        ).fetchone()
        if role_conflict:
            raise ResearchError(
                f"Snapshot de desenvolvimento foi lacrado como holdout da versão {role_conflict['experiment_id']}; trial bloqueado."
            )
        trial_index = existing_count + 1
        trial_id = f"{experiment_id}-trial-{trial_index:03d}"
        config_sha = _sha(config_payload)
        connection.execute(
            """INSERT INTO research_trials
               (trial_id,experiment_id,trial_index,status,backtest_run_id,config_json,config_sha256,
                trial_manifest_path,candidate_status,created_at_utc,finished_at_utc)
               VALUES (?,?,?,'RUNNING',NULL,?,?,NULL,NULL,?,NULL)""",
            (trial_id, experiment_id, trial_index, _canonical(config_payload), config_sha, _now()),
        )
        _event(connection, experiment_id, "TRIAL_STARTED", {
            "trial_id": trial_id, "trial_index": trial_index, "config_sha256": config_sha,
        })

    try:
        snapshot = load_backtest_snapshot(root, experiment["development_snapshot_id"])
        development_extent = _snapshot_extent(root, snapshot.snapshot_id)
        backtest_run = run_backtest(snapshot, config)
        run_artifacts = write_backtest_artifacts(root / "artifacts" / "backtests", backtest_run)
        wf = spec["walk_forward"]
        folds, exclusions = build_walk_forward_folds(
            snapshot,
            decision_timeframe=config.decision_timeframe,
            train_bars=int(wf["train_bars"]),
            test_bars=int(wf["test_bars"]),
            max_folds=int(wf["max_folds"]),
        )
        fold_results: list[dict[str, Any]] = []
        for fold in folds:
            train_start = _utc(str(fold["train_start_at_utc"]))
            train_end = _utc(str(fold["train_end_at_utc_exclusive"]))
            test_start = _utc(str(fold["test_start_at_utc"]))
            test_end = _utc(str(fold["test_end_at_utc_exclusive"]))
            train_result = run_backtest_window(
                snapshot, config, start_at_utc=train_start, end_at_utc=train_end,
                evidence_level="WALK_FORWARD",
            )
            test_result = run_backtest_window(
                snapshot, config, start_at_utc=test_start, end_at_utc=test_end,
                evidence_level="WALK_FORWARD",
            )
            fold_results.append({
                **fold,
                "training": _fold_range_metrics(train_result, train_start, train_end),
                "test": _fold_range_metrics(test_result, test_start, test_end),
                "cost_stress": {
                    f"{multiplier}x": _stress_result(snapshot, config, fold, multiplier)
                    for multiplier in (2, 3)
                },
                "warmup_policy": "somente barras anteriores; caixa reinicia em dinheiro no início de cada recorte",
            })
        status, decision = _candidate_status(spec, fold_results)
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "phase": "F5",
            "experiment": {
                "experiment_id": experiment_id,
                "family_id": experiment["family_id"],
                "version": experiment["version"],
                "spec_sha256": experiment["spec_sha256"],
                "status": experiment["status"],
                "primary_metric": spec["primary_metric"],
            },
            "trial_id": trial_id,
            "trial_index": trial_index,
            "created_at_utc": _now(),
            "development_snapshot": {
                "snapshot_id": snapshot.snapshot_id,
                "content_sha256": snapshot.content_sha256,
                "instrument_id": snapshot.instrument_id,
                "data_origin": snapshot.data_origin,
                "coverage_start_at_utc": snapshot.candles_1m[0].open_at_utc.isoformat().replace("+00:00", "Z"),
                "coverage_end_at_utc_exclusive": snapshot.candles_1m[-1].end_at_utc.isoformat().replace("+00:00", "Z"),
                "candle_count": len(snapshot.candles_1m),
            "expected_1m_minutes": development_extent["expected_count"],
            "missing_1m_minutes_in_partitions": development_extent["gap_count"],
            "minimum_partition_coverage_pct": development_extent["coverage_pct"],
            "partition_ids": development_extent["partition_ids"],
            "symbol": development_extent["symbol"],
            },
            "config": config_payload,
            "config_sha256": config_sha,
            "backtest_run_id": run_artifacts["run_id"],
            "backtest_metrics": json.loads((run_artifacts["metrics_json"]).read_text(encoding="utf-8")),
            "split": spec["walk_forward"],
            "folds": fold_results,
            "excluded_segments": exclusions,
            "decision": {"status": status, **decision},
            "holdout": {
                "status": "SELADO",
                "snapshot_id": experiment["holdout_snapshot_id"],
                "period": spec["holdout_period"],
                "opened": False,
            },
            "limitations": [
                "Folds avaliam regras F3 determinísticas com parâmetros fixos; não há treino automático.",
                "Métrica primária e critério vieram do spec congelado antes dos resultados.",
                "Custos 2x/3x multiplicam fee e slippage por lado; não há spread separado.",
                "OHLCV e atraso assumido não representam execução observada.",
            ],
        }
        trial_dir = root / "artifacts" / "research" / experiment_id / trial_id
        trial_dir.mkdir(parents=True, exist_ok=False)
        manifest_path = trial_dir / "trial.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        _write_trial_report(trial_dir / "report.html", manifest)
        with _db(root) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE research_trials SET status='COMPLETE', backtest_run_id=?, trial_manifest_path=?,
                   candidate_status=?, finished_at_utc=? WHERE trial_id=? AND status='RUNNING'""",
                (run_artifacts["run_id"], manifest_path.relative_to(root).as_posix(), status, _now(), trial_id),
            )
            _event(connection, experiment_id, "TRIAL_COMPLETED", {
                "trial_id": trial_id, "config_sha256": config_sha, "status": status,
                "fold_count": len(fold_results), "trial_manifest": manifest_path.relative_to(root).as_posix(),
            })
        return manifest
    except Exception as error:
        with _db(root) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE research_trials SET status='FAILED', finished_at_utc=? WHERE trial_id=? AND status='RUNNING'",
                (_now(), trial_id),
            )
            _event(connection, experiment_id, "TRIAL_FAILED", {"trial_id": trial_id, "error": str(error)[:1000]})
        raise


def freeze_experiment(root: Path, experiment_id: str, trial_id: str) -> dict[str, Any]:
    with _db(root) as connection:
        connection.execute("BEGIN IMMEDIATE")
        experiment = _load_experiment(connection, experiment_id)
        if experiment["status"] != "OPEN":
            raise ResearchError("Somente uma versão OPEN pode ser congelada.")
        trial = connection.execute(
            "SELECT * FROM research_trials WHERE trial_id=? AND experiment_id=? AND status='COMPLETE'",
            (trial_id, experiment_id),
        ).fetchone()
        if trial is None:
            raise ResearchError("Selecione um trial completo desta versão.")
        manifest_path = root / Path(trial["trial_manifest_path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        spec = json.loads(experiment["spec_json"])
        if experiment["holdout_snapshot_id"]:
            selected_config = _as_config(json.loads(trial["config_json"]))
            _validate_holdout_snapshot(
                root,
                experiment["holdout_snapshot_id"],
                _utc(spec["holdout_period"]["start_at_utc"]),
                _utc(spec["holdout_period"]["end_at_utc_exclusive"]),
                selected_config,
            )
        frozen = {
            "trial_id": trial_id,
            "config": json.loads(trial["config_json"]),
            "config_sha256": trial["config_sha256"],
            "candidate_status": trial["candidate_status"],
            "trial_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        }
        connection.execute(
            "UPDATE research_experiments SET status='FROZEN', frozen_trial_id=?, updated_at_utc=? WHERE experiment_id=?",
            (trial_id, _now(), experiment_id),
        )
        event_id = _event(connection, experiment_id, "EXPERIMENT_FROZEN", frozen)
    return {"experiment_id": experiment_id, "status": "FROZEN", "event_id": event_id, **frozen}


def open_holdout(root: Path, experiment_id: str) -> dict[str, Any]:
    with _db(root) as connection:
        experiment = _load_experiment(connection, experiment_id)
        if experiment["status"] != "FROZEN":
            raise ResearchError("Holdout só pode ser aberto uma vez depois de congelar a versão.")
        if not experiment["holdout_snapshot_id"] or not experiment["frozen_trial_id"]:
            raise ResearchError("Holdout selado sem snapshot elegível ou sem trial congelado.")
        spec = json.loads(experiment["spec_json"])
        trial = connection.execute(
            "SELECT * FROM research_trials WHERE trial_id=? AND status='COMPLETE'",
            (experiment["frozen_trial_id"],),
        ).fetchone()
        if trial is None:
            raise ResearchError("Trial congelado não está disponível.")
        config = _as_config(json.loads(trial["config_json"]))
        snapshot_id = experiment["holdout_snapshot_id"]

    snapshot = load_backtest_snapshot(root, snapshot_id)
    start = _utc(spec["holdout_period"]["start_at_utc"])
    end = _utc(spec["holdout_period"]["end_at_utc_exclusive"])
    development_extent = _snapshot_extent(root, experiment["development_snapshot_id"])
    holdout_extent = _snapshot_extent(root, snapshot_id)
    if start < _utc(development_extent["end_at_utc_exclusive"]):
        raise ResearchError("Holdout deixou de ser posterior ao período de desenvolvimento.")
    if holdout_extent["symbol"] != development_extent["symbol"]:
        raise ResearchError("Holdout deixou de corresponder ao instrumento de desenvolvimento.")
    _validate_holdout_snapshot(root, snapshot_id, start, end, config)
    with _db(root) as connection:
        connection.execute("BEGIN IMMEDIATE")
        experiment = _load_experiment(connection, experiment_id)
        if experiment["status"] != "FROZEN":
            raise ResearchError("Holdout já foi aberto por outra solicitação.")
        connection.execute(
            "UPDATE research_experiments SET status='HOLDOUT_OPENED', updated_at_utc=? WHERE experiment_id=?",
            (_now(), experiment_id),
        )
        event_id = _event(connection, experiment_id, "HOLDOUT_OPENED", {
            "snapshot_id": snapshot_id,
            "frozen_trial_id": experiment["frozen_trial_id"],
            "config_sha256": trial["config_sha256"],
            "period": spec["holdout_period"],
            "one_time_access": True,
        })

    try:
        result = run_backtest_window(
            snapshot, config, start_at_utc=start, end_at_utc=end, evidence_level="SEALED_HOLDOUT",
        )
        holdout_window = {
            "test_start_at_utc": spec["holdout_period"]["start_at_utc"],
            "test_end_at_utc_exclusive": spec["holdout_period"]["end_at_utc_exclusive"],
        }
        cost_stress = {
            f"{multiplier}x": _stress_result(
                snapshot, config, holdout_window, multiplier, evidence_level="SEALED_HOLDOUT",
            )
            for multiplier in (2, 3)
        }
        audit = dict(result.audit)
        audit["research_context"] = {
            "experiment_id": experiment_id,
            "frozen_trial_id": trial["trial_id"],
            "spec_sha256": experiment["spec_sha256"],
            "holdout_event_id": event_id,
            "holdout_snapshot_id": snapshot_id,
        }
        result = replace(result, audit=audit)
        run_artifacts = write_backtest_artifacts(root / "artifacts" / "backtests", result)
        held = next(item for item in result.results if item.strategy == config.strategy)
        benchmark = next(item for item in result.results if item.strategy == StrategyName.BUY_HOLD)
        manifest = {
            "schema_version": 1,
            "phase": "F5_HOLDOUT",
            "experiment_id": experiment_id,
            "family_id": experiment["family_id"],
            "version": experiment["version"],
            "spec_sha256": experiment["spec_sha256"],
            "holdout_event_id": event_id,
            "opened_at_utc": _now(),
            "frozen_trial_id": trial["trial_id"],
            "frozen_config_sha256": trial["config_sha256"],
            "snapshot_id": snapshot_id,
            "snapshot_sha256": snapshot.content_sha256,
            "instrument_id": snapshot.instrument_id,
            "period": spec["holdout_period"],
            "metrics": _run_metrics(held),
            "buy_hold_metrics": _run_metrics(benchmark),
            "cost_stress": cost_stress,
            "backtest_run_id": run_artifacts["run_id"],
            "status": "OPENED_ONCE",
            "post_open_edits_allowed": False,
        }
        out = root / "artifacts" / "research" / experiment_id / "holdout.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        with _db(root) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE research_experiments SET holdout_run_id=?, updated_at_utc=? WHERE experiment_id=? AND status='HOLDOUT_OPENED'",
                (run_artifacts["run_id"], _now(), experiment_id),
            )
            _event(connection, experiment_id, "HOLDOUT_EVALUATION_COMPLETED", {
                "holdout_event_id": event_id, "run_id": run_artifacts["run_id"],
                "manifest": out.relative_to(root).as_posix(), "metrics": manifest["metrics"],
            })
        return manifest
    except Exception as error:
        with _db(root) as connection:
            connection.execute("BEGIN IMMEDIATE")
            _event(connection, experiment_id, "HOLDOUT_EVALUATION_FAILED", {
                "holdout_event_id": event_id, "error": str(error)[:1000],
                "note": "Holdout já foi aberto; retry bloqueado para preservar avaliação única.",
            })
        raise


def list_experiments(root: Path) -> list[dict[str, Any]]:
    with _db(root) as connection:
        rows = connection.execute(
            "SELECT * FROM research_experiments ORDER BY created_at_utc DESC, experiment_id"
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            spec = json.loads(row["spec_json"])
            trials = connection.execute(
                "SELECT trial_id,trial_index,status,backtest_run_id,candidate_status,trial_manifest_path FROM research_trials WHERE experiment_id=? ORDER BY trial_index",
                (row["experiment_id"],),
            ).fetchall()
            result.append({
                "experiment_id": row["experiment_id"],
                "family_id": row["family_id"],
                "version": row["version"],
                "status": row["status"],
                "spec_sha256": row["spec_sha256"],
                "development_snapshot_id": row["development_snapshot_id"],
                "holdout_snapshot_id": row["holdout_snapshot_id"],
                "frozen_trial_id": row["frozen_trial_id"],
                "holdout_run_id": row["holdout_run_id"],
                "spec": spec,
                "trials": [dict(trial) for trial in trials],
            })
    return result


def load_trial_manifest(root: Path, trial_id: str) -> dict[str, Any]:
    for experiment in list_experiments(root):
        for trial in experiment["trials"]:
            if trial["trial_id"] == trial_id and trial["trial_manifest_path"]:
                return json.loads((root / Path(trial["trial_manifest_path"])).read_text(encoding="utf-8"))
    raise ResearchError(f"Trial manifest não encontrado: {trial_id}.")


def list_research_events(root: Path, experiment_id: str) -> list[dict[str, Any]]:
    with _db(root) as connection:
        _load_experiment(connection, experiment_id)
        rows = connection.execute(
            "SELECT event_id,event_type,occurred_at_utc,details_json FROM research_events WHERE experiment_id=? ORDER BY occurred_at_utc,event_id",
            (experiment_id,),
        ).fetchall()
    return [
        {
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "occurred_at_utc": row["occurred_at_utc"],
            "details": json.loads(row["details_json"]),
        }
        for row in rows
    ]
