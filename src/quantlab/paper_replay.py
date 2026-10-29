"""F7: historical, offline paper replay with a durable idempotent journal.

The replay advances one closed 1m candle at a time. Strategy evaluation only
receives complete decision candles whose end is at or before the simulated
clock. Fills use the F3 ledger and its first-1m-open-strictly-after-available
policy; this module adds fail-closed risk gates and durable checkpoints.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator, Mapping
from uuid import uuid4

from quantlab.backtest import (
    BacktestConfig,
    BacktestSnapshot,
    DecisionCandle,
    StrategyResult,
    TIMEFRAME_MINUTES,
    _strictly_next_minute_open,
    run_backtest_window,
)
from quantlab.domain import Candle, PortfolioState, Side, SimulatedFill, StrategyName
from quantlab.engine import SIMULATED_BASE_QTY_STEP
from quantlab.ledger import execute_market_target, mark_equity
from quantlab.research import ResearchError, ensure_snapshot_available

ZERO_HASH = "0" * 64
RULE_STRATEGIES = frozenset({
    StrategyName.CASH,
    StrategyName.BUY_HOLD,
    StrategyName.SMA_CROSS,
    StrategyName.BREAKOUT,
})
MINUTE = timedelta(minutes=1)


class ReplayStatus(StrEnum):
    STOPPED = "STOPPED"
    READY = "READY"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    HALTED = "HALTED"


class ReplayError(ValueError):
    """User-correctable replay request or persisted-state error."""


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_exposure_fraction: Decimal = Decimal("1")
    max_drawdown_pct: Decimal = Decimal("30")

    def __post_init__(self) -> None:
        for name in ("max_exposure_fraction", "max_drawdown_pct"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{name} deve ser um Decimal finito")
        if not Decimal("0") < self.max_exposure_fraction <= Decimal("1"):
            raise ValueError("max_exposure_fraction deve estar entre 0 e 1")
        if not Decimal("0") < self.max_drawdown_pct <= Decimal("100"):
            raise ValueError("max_drawdown_pct deve estar entre 0 e 100")


@dataclass(frozen=True, slots=True)
class StrategyView:
    """Causal strategy input: all decision candles end no later than as_of."""

    as_of_utc: datetime
    decision_candles: tuple[DecisionCandle, ...]

    def __post_init__(self) -> None:
        _require_utc(self.as_of_utc, "as_of_utc")
        if any(item.end_at_utc > self.as_of_utc for item in self.decision_candles):
            raise ValueError("StrategyView contém candle posterior ao relógio simulado")
        if any(
            later.end_at_utc <= earlier.end_at_utc
            for earlier, later in zip(self.decision_candles, self.decision_candles[1:])
        ):
            raise ValueError("StrategyView precisa de candles em ordem temporal")


@dataclass(frozen=True, slots=True)
class ReplaySession:
    replay_id: str
    snapshot_id: str
    snapshot_sha256: str
    config: BacktestConfig
    config_sha256: str
    risk_limits: RiskLimits
    status: ReplayStatus
    state_reason: str
    processed_index: int
    simulated_at_utc: datetime | None
    cash_usdt: Decimal
    base_qty: Decimal
    fees_paid_usdt: Decimal
    current_equity_usdt: Decimal
    high_water_usdt: Decimal
    max_drawdown_pct: Decimal
    data_origin: str
    parent_replay_id: str | None
    comparison: Mapping[str, Any] | None


@contextmanager
def _db(root: Path) -> Iterator[sqlite3.Connection]:
    path = root / "state" / "quantlab.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS paper_replays (
            replay_id TEXT PRIMARY KEY,
            snapshot_id TEXT NOT NULL,
            snapshot_sha256 TEXT NOT NULL,
            config_json TEXT NOT NULL,
            config_sha256 TEXT NOT NULL,
            risk_limits_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('STOPPED','READY','RUNNING','DEGRADED','HALTED')),
            state_reason TEXT NOT NULL,
            processed_index INTEGER NOT NULL,
            simulated_at_utc TEXT,
            initial_cash_usdt TEXT NOT NULL,
            cash_usdt TEXT NOT NULL,
            base_qty TEXT NOT NULL,
            fees_paid_usdt TEXT NOT NULL,
            current_equity_usdt TEXT NOT NULL,
            high_water_usdt TEXT NOT NULL,
            max_drawdown_pct TEXT NOT NULL,
            data_origin TEXT NOT NULL,
            parent_replay_id TEXT,
            last_event_seq INTEGER NOT NULL,
            event_chain_sha256 TEXT NOT NULL,
            comparison_json TEXT,
            compared_at_utc TEXT,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS paper_replay_events (
            event_id TEXT PRIMARY KEY,
            replay_id TEXT NOT NULL REFERENCES paper_replays(replay_id),
            sequence INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            occurred_at_utc TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            previous_hash TEXT NOT NULL,
            event_hash TEXT NOT NULL,
            UNIQUE(replay_id, sequence)
        );
        CREATE INDEX IF NOT EXISTS paper_replay_events_order
            ON paper_replay_events(replay_id, sequence);
        CREATE TABLE IF NOT EXISTS paper_replay_signals (
            signal_id TEXT PRIMARY KEY,
            replay_id TEXT NOT NULL REFERENCES paper_replays(replay_id),
            generated_at_utc TEXT NOT NULL,
            available_at_utc TEXT NOT NULL,
            eligible_at_utc TEXT NOT NULL,
            target_fraction TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL,
            veto_reason TEXT,
            decision_at_utc TEXT NOT NULL,
            UNIQUE(replay_id, generated_at_utc)
        );
        CREATE TABLE IF NOT EXISTS paper_replay_fills (
            event_id TEXT PRIMARY KEY,
            replay_id TEXT NOT NULL REFERENCES paper_replays(replay_id),
            signal_id TEXT NOT NULL UNIQUE REFERENCES paper_replay_signals(signal_id),
            bar_index INTEGER NOT NULL,
            timestamp_utc TEXT NOT NULL,
            target_fraction TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity_base TEXT NOT NULL,
            price_usdt TEXT NOT NULL,
            quote_amount_usdt TEXT NOT NULL,
            fee_usdt TEXT NOT NULL,
            cash_after_usdt TEXT NOT NULL,
            base_qty_after TEXT NOT NULL,
            reference_price_usdt TEXT NOT NULL,
            slippage_cost_usdt TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS paper_replay_fills_order
            ON paper_replay_fills(replay_id, bar_index, timestamp_utc);
        CREATE TABLE IF NOT EXISTS paper_replay_equity (
            replay_id TEXT NOT NULL REFERENCES paper_replays(replay_id),
            bar_index INTEGER NOT NULL,
            timestamp_utc TEXT NOT NULL,
            cash_usdt TEXT NOT NULL,
            base_qty TEXT NOT NULL,
            mark_price_usdt TEXT NOT NULL,
            equity_usdt TEXT NOT NULL,
            fees_paid_usdt TEXT NOT NULL,
            high_water_usdt TEXT NOT NULL,
            drawdown_pct TEXT NOT NULL,
            PRIMARY KEY(replay_id, bar_index)
        );
        CREATE TABLE IF NOT EXISTS paper_replay_decisions (
            replay_id TEXT NOT NULL REFERENCES paper_replays(replay_id),
            decision_at_utc TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY(replay_id, decision_at_utc)
        );
        CREATE TABLE IF NOT EXISTS paper_replay_integrity_incidents (
            incident_id TEXT PRIMARY KEY,
            replay_id TEXT NOT NULL REFERENCES paper_replays(replay_id),
            detected_at_utc TEXT NOT NULL,
            reason TEXT NOT NULL,
            details_json TEXT NOT NULL
        );
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


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} deve ter timezone UTC")


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_time(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))


def _decimal(value: Decimal) -> str:
    return format(value, "f")


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _config_dict(config: BacktestConfig) -> dict[str, object]:
    return {
        "strategy": config.strategy.value,
        "strategy_version": "F3-v1",
        "decision_timeframe": config.decision_timeframe,
        "initial_cash_usdt": _decimal(config.initial_cash_usdt),
        "fee_bps_per_side": _decimal(config.fee_bps_per_side),
        "slippage_bps_per_side": _decimal(config.slippage_bps_per_side),
        "assumed_data_delay_seconds": _decimal(config.assumed_data_delay_seconds),
        "sma_fast": config.sma_fast,
        "sma_slow": config.sma_slow,
        "breakout_lookback": config.breakout_lookback,
        "sizing_base_qty_step": _decimal(SIMULATED_BASE_QTY_STEP),
    }


def _config_from_dict(value: Mapping[str, Any]) -> BacktestConfig:
    return BacktestConfig(
        strategy=StrategyName(value["strategy"]),
        decision_timeframe=str(value["decision_timeframe"]),
        initial_cash_usdt=Decimal(str(value["initial_cash_usdt"])),
        fee_bps_per_side=Decimal(str(value["fee_bps_per_side"])),
        slippage_bps_per_side=Decimal(str(value["slippage_bps_per_side"])),
        assumed_data_delay_seconds=Decimal(str(value["assumed_data_delay_seconds"])),
        sma_fast=int(value["sma_fast"]),
        sma_slow=int(value["sma_slow"]),
        breakout_lookback=int(value["breakout_lookback"]),
    )


def _limits_dict(limits: RiskLimits) -> dict[str, str]:
    return {
        "max_exposure_fraction": _decimal(limits.max_exposure_fraction),
        "max_drawdown_pct": _decimal(limits.max_drawdown_pct),
    }


def _limits_from_dict(value: Mapping[str, Any]) -> RiskLimits:
    return RiskLimits(
        max_exposure_fraction=Decimal(str(value["max_exposure_fraction"])),
        max_drawdown_pct=Decimal(str(value["max_drawdown_pct"])),
    )


def _load_row(connection: sqlite3.Connection, replay_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM paper_replays WHERE replay_id=?", (replay_id,)
    ).fetchone()
    if row is None:
        raise ReplayError(f"Replay não encontrado: {replay_id}")
    return row


def _append_event(
    connection: sqlite3.Connection,
    replay_id: str,
    event_type: str,
    occurred_at_utc: datetime,
    payload: Mapping[str, Any],
    *,
    event_id: str | None = None,
) -> str:
    _require_utc(occurred_at_utc, "occurred_at_utc")
    row = _load_row(connection, replay_id)
    sequence = int(row["last_event_seq"]) + 1
    event_id = event_id or f"{replay_id}:event:{uuid4().hex}"
    payload_json = _canonical(dict(payload))
    occurred = _iso(occurred_at_utc)
    previous_hash = str(row["event_chain_sha256"])
    event_hash = hashlib.sha256(
        _canonical({
            "event_id": event_id,
            "sequence": sequence,
            "event_type": event_type,
            "occurred_at_utc": occurred,
            "payload_json": payload_json,
            "previous_hash": previous_hash,
        }).encode("utf-8")
    ).hexdigest()
    connection.execute(
        """INSERT INTO paper_replay_events
           (event_id,replay_id,sequence,event_type,occurred_at_utc,payload_json,previous_hash,event_hash)
           VALUES(?,?,?,?,?,?,?,?)""",
        (event_id, replay_id, sequence, event_type, occurred, payload_json, previous_hash, event_hash),
    )
    connection.execute(
        "UPDATE paper_replays SET last_event_seq=?,event_chain_sha256=? WHERE replay_id=?",
        (sequence, event_hash, replay_id),
    )
    return event_id


_ALLOWED_TRANSITIONS: dict[ReplayStatus, frozenset[ReplayStatus]] = {
    ReplayStatus.STOPPED: frozenset({ReplayStatus.READY}),
    ReplayStatus.READY: frozenset({ReplayStatus.RUNNING, ReplayStatus.STOPPED, ReplayStatus.HALTED}),
    ReplayStatus.RUNNING: frozenset({ReplayStatus.READY, ReplayStatus.STOPPED, ReplayStatus.DEGRADED, ReplayStatus.HALTED}),
    ReplayStatus.DEGRADED: frozenset({ReplayStatus.STOPPED, ReplayStatus.HALTED}),
    ReplayStatus.HALTED: frozenset(),
}


def _transition_tx(
    connection: sqlite3.Connection,
    replay_id: str,
    target: ReplayStatus,
    reason: str,
    *,
    details: Mapping[str, Any] | None = None,
) -> None:
    row = _load_row(connection, replay_id)
    current = ReplayStatus(row["status"])
    if current is target:
        connection.execute(
            "UPDATE paper_replays SET state_reason=?,updated_at_utc=? WHERE replay_id=?",
            (reason, _iso(datetime.now(UTC)), replay_id),
        )
        return
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise ReplayError(f"Transição de replay não permitida: {current.value} -> {target.value}")
    at = datetime.now(UTC)
    _append_event(connection, replay_id, "STATE_TRANSITION", at, {
        "from": current.value,
        "to": target.value,
        "reason": reason,
        "details": dict(details or {}),
    })
    connection.execute(
        "UPDATE paper_replays SET status=?,state_reason=?,updated_at_utc=? WHERE replay_id=?",
        (target.value, reason, _iso(at), replay_id),
    )


def _halt_tx(
    connection: sqlite3.Connection,
    replay_id: str,
    reason: str,
    details: Mapping[str, Any],
    *,
    event_type: str = "RISK_VETO",
) -> None:
    row = _load_row(connection, replay_id)
    now = _parse_time(row["simulated_at_utc"]) or datetime.now(UTC)
    _append_event(connection, replay_id, event_type, now, {"reason": reason, **dict(details)})
    if ReplayStatus(row["status"]) is not ReplayStatus.HALTED:
        _transition_tx(connection, replay_id, ReplayStatus.HALTED, reason, details=details)


def create_paper_replay(
    root: Path,
    snapshot: BacktestSnapshot,
    config: BacktestConfig,
    risk_limits: RiskLimits | None = None,
    *,
    start_running: bool = True,
    parent_replay_id: str | None = None,
) -> ReplaySession:
    """Freeze one F3 rule configuration and start a new isolated replay."""
    if snapshot.qa_status != "PASS":
        raise ReplayError("RiskGate: snapshot QA FAIL/UNKNOWN bloqueia o replay.")
    catalog_path = root / "state" / "quantlab.sqlite"
    if catalog_path.is_file():
        try:
            ensure_snapshot_available(root, snapshot.snapshot_id)
        except ResearchError as error:
            raise ReplayError(str(error)) from error
    if config.strategy not in RULE_STRATEGIES:
        raise ReplayError("F7 aceita somente regras F3; previsões ML pré-computadas não são replay causal.")
    limits = risk_limits or RiskLimits()
    config_value = _config_dict(config)
    config_json = _canonical(config_value)
    limits_json = _canonical(_limits_dict(limits))
    created_at = _iso(datetime.now(UTC))
    replay_id = f"replay-{uuid4().hex}"
    with _db(root) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO paper_replays
               (replay_id,snapshot_id,snapshot_sha256,config_json,config_sha256,risk_limits_json,
                status,state_reason,processed_index,simulated_at_utc,initial_cash_usdt,cash_usdt,
                base_qty,fees_paid_usdt,current_equity_usdt,high_water_usdt,max_drawdown_pct,
                data_origin,parent_replay_id,last_event_seq,event_chain_sha256,created_at_utc,updated_at_utc)
               VALUES(?,?,?,?,?,?,'STOPPED','REPLAY_CREATED',-1,NULL,?,?, '0','0',?,?,'0',?,?, -1,?,?,?)""",
            (
                replay_id,
                snapshot.snapshot_id,
                snapshot.content_sha256,
                config_json,
                _sha(config_value),
                limits_json,
                _decimal(config.initial_cash_usdt),
                _decimal(config.initial_cash_usdt),
                _decimal(config.initial_cash_usdt),
                _decimal(config.initial_cash_usdt),
                snapshot.data_origin,
                parent_replay_id,
                ZERO_HASH,
                created_at,
                created_at,
            ),
        )
        _transition_tx(connection, replay_id, ReplayStatus.READY, "ESTRATEGIA_E_SNAPSHOT_CONGELADOS")
        if parent_replay_id:
            _append_event(connection, replay_id, "RESET_FROM_REPLAY", datetime.now(UTC), {
                "parent_replay_id": parent_replay_id,
                "reason": "reset explícito em nova sessão; sessão anterior preservada",
            })
        if start_running:
            _transition_tx(connection, replay_id, ReplayStatus.RUNNING, "REPLAY_INICIADO_PELO_USUARIO")
    return get_paper_replay(root, replay_id)


def get_paper_replay(root: Path, replay_id: str) -> ReplaySession:
    with _db(root) as connection:
        row = _load_row(connection, replay_id)
        return _session_from_row(row)


def list_paper_replays(root: Path) -> list[ReplaySession]:
    with _db(root) as connection:
        rows = connection.execute(
            "SELECT * FROM paper_replays ORDER BY created_at_utc DESC,replay_id DESC"
        ).fetchall()
    return [_session_from_row(row) for row in rows]


def _session_from_row(row: sqlite3.Row) -> ReplaySession:
    config_value = json.loads(row["config_json"])
    if _sha(config_value) != row["config_sha256"]:
        raise ReplayError("Config congelada do replay diverge do hash persistido.")
    return ReplaySession(
        replay_id=row["replay_id"],
        snapshot_id=row["snapshot_id"],
        snapshot_sha256=row["snapshot_sha256"],
        config=_config_from_dict(config_value),
        config_sha256=row["config_sha256"],
        risk_limits=_limits_from_dict(json.loads(row["risk_limits_json"])),
        status=ReplayStatus(row["status"]),
        state_reason=row["state_reason"],
        processed_index=int(row["processed_index"]),
        simulated_at_utc=_parse_time(row["simulated_at_utc"]),
        cash_usdt=Decimal(row["cash_usdt"]),
        base_qty=Decimal(row["base_qty"]),
        fees_paid_usdt=Decimal(row["fees_paid_usdt"]),
        current_equity_usdt=Decimal(row["current_equity_usdt"]),
        high_water_usdt=Decimal(row["high_water_usdt"]),
        max_drawdown_pct=Decimal(row["max_drawdown_pct"]),
        data_origin=row["data_origin"],
        parent_replay_id=row["parent_replay_id"],
        comparison=json.loads(row["comparison_json"]) if row["comparison_json"] else None,
    )


def _verify_chain_tx(connection: sqlite3.Connection, row: sqlite3.Row) -> tuple[bool, str]:
    previous = ZERO_HASH
    expected_sequence = 0
    for event in connection.execute(
        "SELECT * FROM paper_replay_events WHERE replay_id=? ORDER BY sequence",
        (row["replay_id"],),
    ):
        if int(event["sequence"]) != expected_sequence or event["previous_hash"] != previous:
            return False, "EVENT_JOURNAL_SEQUENCE_OR_PREVIOUS_HASH_MISMATCH"
        event_hash = hashlib.sha256(
            _canonical({
                "event_id": event["event_id"],
                "sequence": int(event["sequence"]),
                "event_type": event["event_type"],
                "occurred_at_utc": event["occurred_at_utc"],
                "payload_json": event["payload_json"],
                "previous_hash": previous,
            }).encode("utf-8")
        ).hexdigest()
        if event_hash != event["event_hash"]:
            return False, "EVENT_JOURNAL_HASH_MISMATCH"
        previous = event_hash
        expected_sequence += 1
    if expected_sequence - 1 != int(row["last_event_seq"]) or previous != row["event_chain_sha256"]:
        return False, "CHECKPOINT_EVENT_HASH_MISMATCH"
    return True, "OK"


def _same_fill(expected: SimulatedFill, row: sqlite3.Row) -> bool:
    return (
        _iso(expected.timestamp_utc) == row["timestamp_utc"]
        and expected.side.value == row["side"]
        and _decimal(expected.quantity_base) == row["quantity_base"]
        and _decimal(expected.price_usdt) == row["price_usdt"]
        and _decimal(expected.quote_amount_usdt) == row["quote_amount_usdt"]
        and _decimal(expected.fee_usdt) == row["fee_usdt"]
        and _decimal(expected.cash_after_usdt) == row["cash_after_usdt"]
        and _decimal(expected.base_qty_after) == row["base_qty_after"]
        and _decimal(expected.reference_price_usdt) == row["reference_price_usdt"]
        and _decimal(expected.slippage_cost_usdt) == row["slippage_cost_usdt"]
    )


def _reconcile_tx(
    connection: sqlite3.Connection,
    replay_id: str,
    snapshot: BacktestSnapshot,
) -> dict[str, Any]:
    row = _load_row(connection, replay_id)
    if snapshot.snapshot_id != row["snapshot_id"] or snapshot.content_sha256 != row["snapshot_sha256"]:
        reason = "SNAPSHOT_ID_OR_HASH_DIVERGENCE"
        _halt_tx(connection, replay_id, reason, {
            "expected_snapshot_id": row["snapshot_id"],
            "received_snapshot_id": snapshot.snapshot_id,
        }, event_type="INTEGRITY_FAILURE")
        return {"ok": False, "reason": reason}
    chain_ok, chain_reason = _verify_chain_tx(connection, row)
    if not chain_ok:
        incident_id = f"incident-{uuid4().hex}"
        details = {"reason": chain_reason, "event_count": int(row["last_event_seq"]) + 1}
        connection.execute(
            "INSERT INTO paper_replay_integrity_incidents VALUES(?,?,?,?,?)",
            (incident_id, replay_id, _iso(datetime.now(UTC)), chain_reason, _canonical(details)),
        )
        connection.execute(
            "UPDATE paper_replays SET status='HALTED',state_reason=?,updated_at_utc=? WHERE replay_id=?",
            (chain_reason, _iso(datetime.now(UTC)), replay_id),
        )
        return {"ok": False, "reason": chain_reason, "incident_id": incident_id}

    event_rows = connection.execute(
        "SELECT event_id,event_type,payload_json FROM paper_replay_events WHERE replay_id=?",
        (replay_id,),
    ).fetchall()
    events_by_id = {item["event_id"]: item for item in event_rows}
    signal_rows = connection.execute(
        "SELECT * FROM paper_replay_signals WHERE replay_id=? ORDER BY generated_at_utc,signal_id",
        (replay_id,),
    ).fetchall()
    for signal in signal_rows:
        event = events_by_id.get(signal["signal_id"])
        if event is None or event["event_type"] != "SIGNAL_CREATED":
            return _ledger_divergence_tx(connection, replay_id, "SIGNAL_JOURNAL_DIVERGENCE", {
                "signal_id": signal["signal_id"],
            })
        payload = json.loads(event["payload_json"])
        signal_values = {
            "signal_id": signal["signal_id"],
            "generated_at_utc": signal["generated_at_utc"],
            "available_at_utc": signal["available_at_utc"],
            "eligible_at_utc": signal["eligible_at_utc"],
            "target_fraction": signal["target_fraction"],
            "reason": signal["reason"],
            "decision_at_utc": signal["decision_at_utc"],
        }
        if payload != signal_values:
            return _ledger_divergence_tx(connection, replay_id, "SIGNAL_JOURNAL_PAYLOAD_MISMATCH", {
                "signal_id": signal["signal_id"],
            })
    fill_events = [item for item in event_rows if item["event_type"] == "FILL"]
    fill_rows = connection.execute(
        "SELECT * FROM paper_replay_fills WHERE replay_id=? ORDER BY bar_index,timestamp_utc,event_id",
        (replay_id,),
    ).fetchall()
    if {item["event_id"] for item in fill_events} != {item["event_id"] for item in fill_rows}:
        return _ledger_divergence_tx(connection, replay_id, "FILL_JOURNAL_EVENT_SET_MISMATCH", {
            "fill_events": len(fill_events), "fill_rows": len(fill_rows),
        })
    filled_signal_ids = {item["signal_id"] for item in fill_rows}
    if filled_signal_ids != {item["signal_id"] for item in signal_rows if item["status"] == "FILLED"}:
        return _ledger_divergence_tx(connection, replay_id, "SIGNAL_FILL_STATUS_MISMATCH", {})

    processed_index = int(row["processed_index"])
    if processed_index < -1 or processed_index >= len(snapshot.candles_1m):
        return _ledger_divergence_tx(connection, replay_id, "CHECKPOINT_INDEX_OUT_OF_RANGE", {})
    equity_rows = connection.execute(
        "SELECT * FROM paper_replay_equity WHERE replay_id=? ORDER BY bar_index", (replay_id,)
    ).fetchall()
    if len(equity_rows) != processed_index + 1:
        return _ledger_divergence_tx(connection, replay_id, "CHECKPOINT_EQUITY_COUNT_MISMATCH", {
            "processed_index": processed_index,
            "equity_rows": len(equity_rows),
        })

    config = _config_from_dict(json.loads(row["config_json"]))
    state = PortfolioState(cash_usdt=config.initial_cash_usdt, base_qty=Decimal("0"))
    fee_rate = config.fee_bps_per_side / Decimal("10000")
    slip_rate = config.slippage_bps_per_side / Decimal("10000")
    fills_by_index: dict[int, list[sqlite3.Row]] = {}
    for fill_row in fill_rows:
        fills_by_index.setdefault(int(fill_row["bar_index"]), []).append(fill_row)
    high_water = config.initial_cash_usdt
    worst_drawdown = Decimal("0")
    current_equity = config.initial_cash_usdt
    simulated_at: datetime | None = None
    for index, equity_row in enumerate(equity_rows):
        candle = snapshot.candles_1m[index]
        if int(equity_row["bar_index"]) != index or equity_row["timestamp_utc"] != _iso(candle.end_at_utc):
            return _ledger_divergence_tx(connection, replay_id, "CHECKPOINT_TIME_OR_INDEX_MISMATCH", {"bar_index": index})
        if index and candle.open_at_utc != snapshot.candles_1m[index - 1].end_at_utc:
            return _ledger_divergence_tx(connection, replay_id, "CHECKPOINT_CROSSES_MISSING_BAR", {"bar_index": index})
        for fill_row in fills_by_index.pop(index, []):
            signal = connection.execute(
                "SELECT * FROM paper_replay_signals WHERE signal_id=? AND replay_id=?",
                (fill_row["signal_id"], replay_id),
            ).fetchone()
            if signal is None:
                return _ledger_divergence_tx(connection, replay_id, "FILL_WITHOUT_SIGNAL", {"event_id": fill_row["event_id"]})
            target = Decimal(signal["target_fraction"])
            eligible_at = _parse_time(signal["eligible_at_utc"])
            available_at = _parse_time(signal["available_at_utc"])
            if eligible_at != candle.open_at_utc or available_at is None or eligible_at <= available_at:
                return _ledger_divergence_tx(connection, replay_id, "FILL_TEMPORALITY_DIVERGENCE", {"event_id": fill_row["event_id"]})
            state, expected_fill, status = execute_market_target(
                state,
                timestamp_utc=candle.open_at_utc,
                target_fraction=target,
                reference_open_usdt=candle.open,
                fee_rate=fee_rate,
                slippage_rate=slip_rate,
                quantity_step=SIMULATED_BASE_QTY_STEP,
            )
            if status != "FILLED" or expected_fill is None or not _same_fill(expected_fill, fill_row):
                return _ledger_divergence_tx(connection, replay_id, "FILL_LEDGER_REPLAY_MISMATCH", {"event_id": fill_row["event_id"]})
            event_payload = json.loads(events_by_id[fill_row["event_id"]]["payload_json"])
            expected_payload = _fill_payload(expected_fill, signal["signal_id"], index, target)
            if event_payload != expected_payload:
                return _ledger_divergence_tx(connection, replay_id, "FILL_JOURNAL_PAYLOAD_MISMATCH", {
                    "event_id": fill_row["event_id"],
                })
        if state.cash_usdt < 0 or state.base_qty < 0:
            return _ledger_divergence_tx(connection, replay_id, "NEGATIVE_LEDGER_BALANCE", {"bar_index": index})
        current_equity = mark_equity(state, candle.close)
        high_water = max(high_water, current_equity)
        drawdown = (high_water - current_equity) / high_water * Decimal("100") if high_water else Decimal("0")
        worst_drawdown = max(worst_drawdown, drawdown)
        expected_values = (
            _decimal(state.cash_usdt),
            _decimal(state.base_qty),
            _decimal(candle.close),
            _decimal(current_equity),
            _decimal(state.fees_paid_usdt),
            _decimal(high_water),
            _decimal(drawdown),
        )
        persisted_values = tuple(equity_row[key] for key in (
            "cash_usdt", "base_qty", "mark_price_usdt", "equity_usdt", "fees_paid_usdt", "high_water_usdt", "drawdown_pct"
        ))
        if expected_values != persisted_values:
            return _ledger_divergence_tx(connection, replay_id, "EQUITY_RECONCILIATION_MISMATCH", {"bar_index": index})
        simulated_at = candle.end_at_utc
    if fills_by_index:
        return _ledger_divergence_tx(connection, replay_id, "FILL_AFTER_CHECKPOINT", {})
    if processed_index >= 0:
        last_candle = snapshot.candles_1m[processed_index]
        if row["simulated_at_utc"] != _iso(last_candle.end_at_utc):
            return _ledger_divergence_tx(connection, replay_id, "CHECKPOINT_CLOCK_MISMATCH", {})
        checkpoint = connection.execute(
            """SELECT payload_json FROM paper_replay_events
               WHERE replay_id=? AND event_type='CHECKPOINT' ORDER BY sequence DESC LIMIT 1""",
            (replay_id,),
        ).fetchone()
        if checkpoint is None:
            return _ledger_divergence_tx(connection, replay_id, "CHECKPOINT_EVENT_MISSING", {})
        checkpoint_payload = json.loads(checkpoint["payload_json"])
        if (
            int(checkpoint_payload.get("processed_index", -2)) != processed_index
            or checkpoint_payload.get("simulated_at_utc") != _iso(last_candle.end_at_utc)
            or checkpoint_payload.get("cash_usdt") != row["cash_usdt"]
            or checkpoint_payload.get("base_qty") != row["base_qty"]
            or checkpoint_payload.get("fees_paid_usdt") != row["fees_paid_usdt"]
            or checkpoint_payload.get("equity_usdt") != row["current_equity_usdt"]
        ):
            return _ledger_divergence_tx(connection, replay_id, "CHECKPOINT_EVENT_PAYLOAD_MISMATCH", {})
    elif row["simulated_at_utc"] is not None:
        return _ledger_divergence_tx(connection, replay_id, "EMPTY_CHECKPOINT_HAS_CLOCK", {})
    checks = (
        _decimal(state.cash_usdt) == row["cash_usdt"],
        _decimal(state.base_qty) == row["base_qty"],
        _decimal(state.fees_paid_usdt) == row["fees_paid_usdt"],
        _decimal(current_equity) == row["current_equity_usdt"],
        _decimal(high_water) == row["high_water_usdt"],
        _decimal(worst_drawdown) == row["max_drawdown_pct"],
    )
    if not all(checks):
        return _ledger_divergence_tx(connection, replay_id, "LEDGER_CHECKPOINT_DIVERGENCE", {
            "cash_ok": checks[0], "position_ok": checks[1], "fees_ok": checks[2],
            "equity_ok": checks[3], "high_water_ok": checks[4], "drawdown_ok": checks[5],
        })
    return {
        "ok": True,
        "reason": "OK",
        "replayed_fills": len(fill_rows),
        "equity_points": len(equity_rows),
        "cash_usdt": _decimal(state.cash_usdt),
        "base_qty": _decimal(state.base_qty),
        "fees_paid_usdt": _decimal(state.fees_paid_usdt),
        "equity_usdt": _decimal(current_equity),
    }


def _ledger_divergence_tx(
    connection: sqlite3.Connection,
    replay_id: str,
    reason: str,
    details: Mapping[str, Any],
) -> dict[str, Any]:
    row = _load_row(connection, replay_id)
    if ReplayStatus(row["status"]) is not ReplayStatus.HALTED:
        _halt_tx(connection, replay_id, reason, details, event_type="LEDGER_DIVERGENCE")
    return {"ok": False, "reason": reason, "details": dict(details)}


def reconcile_paper_replay(
    root: Path,
    replay_id: str,
    snapshot: BacktestSnapshot,
) -> dict[str, Any]:
    """Re-execute journaled fills and reconcile every durable equity checkpoint."""
    with _db(root) as connection:
        connection.execute("BEGIN IMMEDIATE")
        result = _reconcile_tx(connection, replay_id, snapshot)
    return result


def pause_paper_replay(root: Path, replay_id: str) -> ReplaySession:
    with _db(root) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _transition_tx(connection, replay_id, ReplayStatus.READY, "PAUSADO_PELO_USUARIO")
    return get_paper_replay(root, replay_id)


def resume_paper_replay(
    root: Path,
    replay_id: str,
    snapshot: BacktestSnapshot,
) -> ReplaySession:
    holdout_error: str | None = None
    if (root / "state" / "quantlab.sqlite").is_file():
        try:
            ensure_snapshot_available(root, snapshot.snapshot_id)
        except ResearchError as error:
            holdout_error = str(error)
    with _db(root) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if holdout_error:
            _halt_tx(connection, replay_id, "SNAPSHOT_SEALED_HOLDOUT", {"message": holdout_error})
            return _session_from_row(_load_row(connection, replay_id))
        checked = _reconcile_tx(connection, replay_id, snapshot)
        if not checked["ok"]:
            return _session_from_row(_load_row(connection, replay_id))
        _transition_tx(connection, replay_id, ReplayStatus.RUNNING, "RETOMADO_APOS_RECONCILIACAO")
    return get_paper_replay(root, replay_id)


def stop_paper_replay(root: Path, replay_id: str) -> ReplaySession:
    with _db(root) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _transition_tx(connection, replay_id, ReplayStatus.STOPPED, "ENCERRADO_PELO_USUARIO")
    return get_paper_replay(root, replay_id)


def reset_paper_replay(
    root: Path,
    replay_id: str,
    snapshot: BacktestSnapshot,
    *,
    reviewed_halt: bool = False,
) -> ReplaySession:
    old = get_paper_replay(root, replay_id)
    if old.status is ReplayStatus.RUNNING:
        raise ReplayError("Pause ou pare o replay antes de resetar para uma nova sessão.")
    if old.status is ReplayStatus.HALTED and not reviewed_halt:
        raise ReplayError("RESET de HALTED exige revisão explícita do motivo registrado.")
    if old.snapshot_id != snapshot.snapshot_id or old.snapshot_sha256 != snapshot.content_sha256:
        raise ReplayError("RESET precisa manter o mesmo snapshot imutável da sessão original.")
    return create_paper_replay(
        root,
        snapshot,
        old.config,
        old.risk_limits,
        start_running=False,
        parent_replay_id=old.replay_id,
    )


def _event_at(
    connection: sqlite3.Connection,
    replay_id: str,
    event_type: str,
    at: datetime,
    payload: Mapping[str, Any],
    *,
    event_id: str | None = None,
) -> str:
    return _append_event(connection, replay_id, event_type, at, payload, event_id=event_id)


def _new_signal(
    connection: sqlite3.Connection,
    replay_id: str,
    generated_at: datetime,
    available_at: datetime,
    eligible_at: datetime,
    target: Decimal,
    reason: str,
    decision_at: datetime,
) -> bool:
    signal_id = f"{replay_id}:signal:{_sha([_iso(generated_at), _iso(available_at), _decimal(target)])}"
    existing = connection.execute(
        "SELECT 1 FROM paper_replay_signals WHERE signal_id=?", (signal_id,)
    ).fetchone()
    existing_at_time = connection.execute(
        "SELECT signal_id FROM paper_replay_signals WHERE replay_id=? AND generated_at_utc=?",
        (replay_id, _iso(generated_at)),
    ).fetchone()
    event_exists = connection.execute(
        "SELECT 1 FROM paper_replay_events WHERE event_id=?", (signal_id,)
    ).fetchone()
    if existing or existing_at_time or event_exists:
        _halt_tx(connection, replay_id, "DUPLICATE_EVENT_ID", {
            "event_id": signal_id,
            "existing_signal_id": existing_at_time["signal_id"] if existing_at_time else None,
            "event_kind": "SIGNAL_CREATED",
        }, event_type="RISK_VETO")
        return False
    payload = {
        "signal_id": signal_id,
        "generated_at_utc": _iso(generated_at),
        "available_at_utc": _iso(available_at),
        "eligible_at_utc": _iso(eligible_at),
        "target_fraction": _decimal(target),
        "reason": reason,
        "decision_at_utc": _iso(decision_at),
    }
    _event_at(connection, replay_id, "SIGNAL_CREATED", generated_at, payload, event_id=signal_id)
    connection.execute(
        """INSERT INTO paper_replay_signals
           (signal_id,replay_id,generated_at_utc,available_at_utc,eligible_at_utc,
            target_fraction,reason,status,veto_reason,decision_at_utc)
           VALUES(?,?,?,?,?,?,?,'PENDING',NULL,?)""",
        (
            signal_id,
            replay_id,
            _iso(generated_at),
            _iso(available_at),
            _iso(eligible_at),
            _decimal(target),
            reason,
            _iso(decision_at),
        ),
    )
    return True


def _decision_candle_at(
    candles: tuple[Candle, ...], index: int, timeframe: str
) -> DecisionCandle | None:
    minutes = TIMEFRAME_MINUTES[timeframe]
    width = timedelta(minutes=minutes)
    current = candles[index]
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = current.open_at_utc - epoch
    bucket = epoch + width * (elapsed // width)
    if current.end_at_utc != bucket + width:
        return None
    start_index = index - minutes + 1
    if start_index < 0:
        return None
    members = candles[start_index:index + 1]
    if len(members) != minutes:
        return None
    if any(
        member.open_at_utc != bucket + MINUTE * offset
        or member.end_at_utc != bucket + MINUTE * (offset + 1)
        for offset, member in enumerate(members)
    ):
        return None
    return DecisionCandle(
        open_at_utc=bucket,
        end_at_utc=bucket + width,
        open=members[0].open,
        high=max(member.high for member in members),
        low=min(member.low for member in members),
        close=members[-1].close,
        volume_base=sum((member.volume_base for member in members), start=Decimal("0")),
        constituents=minutes,
    )


def _evaluate_strategy_view(
    strategy: StrategyName,
    config: BacktestConfig,
    view: StrategyView,
) -> tuple[Decimal, str] | None:
    bars = view.decision_candles
    if not bars:
        return None
    current = bars[-1]
    if strategy is StrategyName.SMA_CROSS:
        if len(bars) < config.sma_slow:
            return None
        fast_values = [item.close for item in bars[-config.sma_fast:]]
        slow_values = [item.close for item in bars[-config.sma_slow:]]
        fast_mean = sum(fast_values, start=Decimal("0")) / Decimal(len(fast_values))
        slow_mean = sum(slow_values, start=Decimal("0")) / Decimal(len(slow_values))
        target = Decimal("1") if fast_mean > slow_mean else Decimal("0")
        reason = (
            f"SMA({config.sma_fast}) acima de SMA({config.sma_slow})"
            if target == 1
            else f"SMA({config.sma_fast}) não está acima de SMA({config.sma_slow})"
        )
        return target, reason
    if strategy is StrategyName.BREAKOUT:
        lookback = config.breakout_lookback
        if len(bars) <= lookback:
            return None
        prior_high = max(item.high for item in bars[-lookback - 1:-1])
        target = Decimal("1") if current.close > prior_high else Decimal("0")
        reason = (
            f"close rompe máxima das {lookback} barras completas anteriores"
            if target == 1
            else f"sem rompimento; breakout sai para caixa (regra F3 v1, lookback={lookback})"
        )
        return target, reason
    return None


def _store_decision_and_signal(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    snapshot: BacktestSnapshot,
    config: BacktestConfig,
    index: int,
    candle: Candle,
) -> None:
    decision = _decision_candle_at(snapshot.candles_1m, index, config.decision_timeframe)
    if decision is None:
        return
    decision_payload = {
        "open_at_utc": _iso(decision.open_at_utc),
        "end_at_utc": _iso(decision.end_at_utc),
        "open": _decimal(decision.open),
        "high": _decimal(decision.high),
        "low": _decimal(decision.low),
        "close": _decimal(decision.close),
        "volume_base": _decimal(decision.volume_base),
        "constituents": decision.constituents,
    }
    connection.execute(
        "INSERT INTO paper_replay_decisions VALUES(?,?,?)",
        (row["replay_id"], _iso(decision.end_at_utc), _canonical(decision_payload)),
    )
    # The strategy receives only this bounded prefix from the persisted,
    # complete decision candles. No later row or OHLC value is reachable.
    needed = max(config.sma_slow, config.breakout_lookback + 1)
    rows = connection.execute(
        "SELECT payload_json FROM paper_replay_decisions WHERE replay_id=? ORDER BY decision_at_utc DESC LIMIT ?",
        (row["replay_id"], needed),
    ).fetchall()
    all_bars = [json.loads(item["payload_json"]) for item in reversed(rows)]
    visible = tuple(
        DecisionCandle(
            open_at_utc=_parse_time(item["open_at_utc"]),
            end_at_utc=_parse_time(item["end_at_utc"]),
            open=Decimal(item["open"]),
            high=Decimal(item["high"]),
            low=Decimal(item["low"]),
            close=Decimal(item["close"]),
            volume_base=Decimal(item["volume_base"]),
            constituents=int(item["constituents"]),
        )
        for item in all_bars
    )
    view = StrategyView(as_of_utc=candle.end_at_utc, decision_candles=visible)
    evaluated = _evaluate_strategy_view(config.strategy, config, view)
    if evaluated is None:
        return
    target, reason = evaluated
    delay = timedelta(microseconds=int(config.assumed_data_delay_seconds * Decimal("1000000")))
    available = decision.end_at_utc + delay
    eligible = _strictly_next_minute_open(available)
    _new_signal(
        connection,
        row["replay_id"],
        decision.end_at_utc,
        available,
        eligible,
        target,
        reason,
        decision.end_at_utc,
    )


def _risk_result(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    signal: sqlite3.Row,
    candle: Candle,
    index: int,
    snapshot: BacktestSnapshot,
    state: PortfolioState,
    current_equity: Decimal,
    current_mark: Decimal | None,
    max_drawdown_pct: Decimal,
) -> tuple[str, str | None]:
    if snapshot.qa_status != "PASS":
        return "HALT", "ESSENTIAL_QA_FAIL_OR_UNKNOWN"
    cash = state.cash_usdt
    qty = state.base_qty
    equity = current_equity
    if cash < 0 or qty < 0 or equity < 0:
        return "HALT", "NEGATIVE_LEDGER_BALANCE"
    signal_id = signal["signal_id"]
    fill_id = f"{row['replay_id']}:fill:{_sha(signal_id)}"
    if connection.execute(
        "SELECT 1 FROM paper_replay_events WHERE event_id=? UNION ALL SELECT 1 FROM paper_replay_fills WHERE event_id=? LIMIT 1",
        (fill_id, fill_id),
    ).fetchone():
        return "HALT", "DUPLICATE_EVENT_ID"
    target = Decimal(signal["target_fraction"])
    current_exposure = Decimal("0")
    if equity > 0 and current_mark is not None:
        position_value = qty * current_mark
        if position_value > 0:
            current_exposure = position_value / equity
    is_increasing = target > current_exposure and target > 0
    limits = _limits_from_dict(json.loads(row["risk_limits_json"]))
    if is_increasing and target > limits.max_exposure_fraction:
        return "VETO", "MAX_EXPOSURE_LIMIT"
    if is_increasing and max_drawdown_pct >= limits.max_drawdown_pct:
        return "HALT", "MAX_SIMULATION_DRAWDOWN_REACHED"
    # The same F3 ledger remains the authority for sizing and non-negative cash.
    next_state, _, status = execute_market_target(
        state,
        timestamp_utc=candle.open_at_utc,
        target_fraction=target,
        reference_open_usdt=candle.open,
        fee_rate=_config_from_dict(json.loads(row["config_json"])).fee_bps_per_side / Decimal("10000"),
        slippage_rate=_config_from_dict(json.loads(row["config_json"])).slippage_bps_per_side / Decimal("10000"),
        quantity_step=SIMULATED_BASE_QTY_STEP,
    )
    if next_state.cash_usdt < 0 or next_state.base_qty < 0:
        return "HALT", "NEGATIVE_LEDGER_BALANCE"
    if status == "INSUFFICIENT_CASH":
        return "VETO", "INSUFFICIENT_CASH"
    return "APPROVE", None


def _mark_opening_risk(
    state: PortfolioState,
    opening_price: Decimal,
    high_water_usdt: Decimal,
    previous_max_drawdown_pct: Decimal,
) -> tuple[Decimal, Decimal]:
    """Include an execution bar's opening gap in risk before approving a fill."""
    opening_equity = mark_equity(state, opening_price)
    opening_high_water = max(high_water_usdt, opening_equity)
    opening_drawdown = (
        (opening_high_water - opening_equity) / opening_high_water * Decimal("100")
        if opening_high_water
        else Decimal("0")
    )
    return opening_equity, max(previous_max_drawdown_pct, opening_drawdown)


def advance_paper_replay(
    root: Path,
    replay_id: str,
    snapshot: BacktestSnapshot,
    *,
    max_candles: int = 60,
) -> ReplaySession:
    """Advance a bounded number of historical candles in one SQLite transaction."""
    if not 1 <= max_candles <= 1000:
        raise ValueError("max_candles deve estar entre 1 e 1000")
    with _db(root) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _load_row(connection, replay_id)
        if ReplayStatus(row["status"]) is not ReplayStatus.RUNNING:
            return _session_from_row(row)
        if snapshot.qa_status != "PASS":
            _halt_tx(connection, replay_id, "ESSENTIAL_QA_FAIL_OR_UNKNOWN", {"qa_status": snapshot.qa_status})
            return _session_from_row(_load_row(connection, replay_id))
        if snapshot.snapshot_id != row["snapshot_id"] or snapshot.content_sha256 != row["snapshot_sha256"]:
            _halt_tx(connection, replay_id, "SNAPSHOT_ID_OR_HASH_DIVERGENCE", {
                "received_snapshot_id": snapshot.snapshot_id,
            }, event_type="INTEGRITY_FAILURE")
            return _session_from_row(_load_row(connection, replay_id))
        # Cheap local checkpoint check on every refresh. Full journal replay is
        # done on resume/restart by reconcile_paper_replay.
        previous_index = int(row["processed_index"])
        if previous_index >= 0:
            last = connection.execute(
                "SELECT * FROM paper_replay_equity WHERE replay_id=? AND bar_index=?",
                (replay_id, previous_index),
            ).fetchone()
            if last is None or last["timestamp_utc"] != row["simulated_at_utc"] or last["equity_usdt"] != row["current_equity_usdt"]:
                _ledger_divergence_tx(connection, replay_id, "LATEST_CHECKPOINT_DIVERGENCE", {"bar_index": previous_index})
                return _session_from_row(_load_row(connection, replay_id))
        config = _config_from_dict(json.loads(row["config_json"]))
        if _sha(json.loads(row["config_json"])) != row["config_sha256"]:
            _ledger_divergence_tx(connection, replay_id, "FROZEN_CONFIG_HASH_MISMATCH", {})
            return _session_from_row(_load_row(connection, replay_id))
        state = PortfolioState(
            cash_usdt=Decimal(row["cash_usdt"]),
            base_qty=Decimal(row["base_qty"]),
            fees_paid_usdt=Decimal(row["fees_paid_usdt"]),
        )
        high_water = Decimal(row["high_water_usdt"])
        max_drawdown = Decimal(row["max_drawdown_pct"])
        current_equity = Decimal(row["current_equity_usdt"])
        processed_index = previous_index
        start_index = previous_index + 1
        stop_index = min(start_index + max_candles, len(snapshot.candles_1m))
        for index in range(start_index, stop_index):
            if ReplayStatus(_load_row(connection, replay_id)["status"]) is not ReplayStatus.RUNNING:
                break
            candle = snapshot.candles_1m[index]
            if connection.execute(
                "SELECT 1 FROM paper_replay_equity WHERE replay_id=? AND bar_index=?",
                (replay_id, index),
            ).fetchone():
                _ledger_divergence_tx(connection, replay_id, "DUPLICATE_BAR_CHECKPOINT", {"bar_index": index})
                break
            if index and candle.open_at_utc != snapshot.candles_1m[index - 1].end_at_utc:
                missing_minutes = max(
                    int((candle.open_at_utc - snapshot.candles_1m[index - 1].end_at_utc).total_seconds() // 60), 1
                )
                _halt_tx(connection, replay_id, "GAP_DETECTED", {
                    "last_valid_end_at_utc": _iso(snapshot.candles_1m[index - 1].end_at_utc),
                    "next_observed_open_at_utc": _iso(candle.open_at_utc),
                    "missing_1m_bars": missing_minutes,
                    "new_fills_blocked": True,
                })
                break
            due_signals = connection.execute(
                """SELECT * FROM paper_replay_signals
                   WHERE replay_id=? AND status='PENDING' AND eligible_at_utc<=?
                   ORDER BY eligible_at_utc,signal_id""",
                (replay_id, _iso(candle.open_at_utc)),
            ).fetchall()
            for signal in due_signals:
                eligible_at = _parse_time(signal["eligible_at_utc"])
                if eligible_at != candle.open_at_utc:
                    _halt_tx(connection, replay_id, "MISSING_ELIGIBLE_EXECUTION_BAR", {
                        "signal_id": signal["signal_id"],
                        "eligible_at_utc": signal["eligible_at_utc"],
                        "observed_open_at_utc": _iso(candle.open_at_utc),
                    })
                    break
                row = _load_row(connection, replay_id)
                # Revalue at the actual execution open before permitting a buy;
                # a gap down may cross the drawdown limit since the prior close.
                opening_equity, opening_drawdown = _mark_opening_risk(
                    state, candle.open, high_water, max_drawdown,
                )
                opening_high_water = max(high_water, opening_equity)
                observed_opening_drawdown = (
                    (opening_high_water - opening_equity) / opening_high_water * Decimal("100")
                    if opening_high_water else Decimal("0")
                )
                decision, risk_reason = _risk_result(
                    connection, row, signal, candle, index, snapshot, state,
                    opening_equity, candle.open, opening_drawdown,
                )
                if decision == "HALT":
                    opening_limits = _limits_from_dict(json.loads(row["risk_limits_json"]))
                    _halt_tx(connection, replay_id, risk_reason or "RISK_GATE_HALTED", {
                        "signal_id": signal["signal_id"],
                        "event_id": f"{replay_id}:fill:{_sha(signal['signal_id'])}",
                        "bar_index": index,
                        "opening_mark_usdt": _decimal(candle.open),
                        "opening_equity_usdt": _decimal(opening_equity),
                        "opening_drawdown_pct": _decimal(observed_opening_drawdown),
                        "effective_drawdown_pct": _decimal(opening_drawdown),
                        "drawdown_limit_pct": _decimal(opening_limits.max_drawdown_pct),
                    })
                    break
                if decision == "VETO":
                    _event_at(connection, replay_id, "RISK_VETO", candle.open_at_utc, {
                        "signal_id": signal["signal_id"],
                        "reason": risk_reason,
                        "new_fill_created": False,
                    })
                    connection.execute(
                        "UPDATE paper_replay_signals SET status=?,veto_reason=? WHERE signal_id=?",
                        (f"VETOED_{risk_reason}", risk_reason, signal["signal_id"]),
                    )
                    continue
                state, fill, status = execute_market_target(
                    state,
                    timestamp_utc=candle.open_at_utc,
                    target_fraction=Decimal(signal["target_fraction"]),
                    reference_open_usdt=candle.open,
                    fee_rate=config.fee_bps_per_side / Decimal("10000"),
                    slippage_rate=config.slippage_bps_per_side / Decimal("10000"),
                    quantity_step=SIMULATED_BASE_QTY_STEP,
                )
                if status == "TARGET_UNCHANGED":
                    _event_at(connection, replay_id, "SIGNAL_NO_ORDER", candle.open_at_utc, {
                        "signal_id": signal["signal_id"], "reason": "TARGET_UNCHANGED",
                    })
                    connection.execute(
                        "UPDATE paper_replay_signals SET status='NO_ORDER_TARGET_UNCHANGED' WHERE signal_id=?",
                        (signal["signal_id"],),
                    )
                    continue
                if status != "FILLED" or fill is None:
                    _halt_tx(connection, replay_id, "F3_LEDGER_RESULT_UNKNOWN", {
                        "signal_id": signal["signal_id"], "ledger_status": status,
                    }, event_type="LEDGER_DIVERGENCE")
                    break
                if state.cash_usdt < 0 or state.base_qty < 0:
                    _halt_tx(connection, replay_id, "NEGATIVE_LEDGER_BALANCE", {
                        "signal_id": signal["signal_id"], "bar_index": index,
                    }, event_type="LEDGER_DIVERGENCE")
                    break
                fill_id = f"{replay_id}:fill:{_sha(signal['signal_id'])}"
                fill_payload = _fill_payload(fill, signal["signal_id"], index, Decimal(signal["target_fraction"]))
                _event_at(connection, replay_id, "FILL", fill.timestamp_utc, fill_payload, event_id=fill_id)
                connection.execute(
                    """INSERT INTO paper_replay_fills
                       (event_id,replay_id,signal_id,bar_index,timestamp_utc,target_fraction,side,
                        quantity_base,price_usdt,quote_amount_usdt,fee_usdt,cash_after_usdt,
                        base_qty_after,reference_price_usdt,slippage_cost_usdt)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        fill_id, replay_id, signal["signal_id"], index, _iso(fill.timestamp_utc),
                        _decimal(Decimal(signal["target_fraction"])), fill.side.value,
                        _decimal(fill.quantity_base), _decimal(fill.price_usdt), _decimal(fill.quote_amount_usdt),
                        _decimal(fill.fee_usdt), _decimal(fill.cash_after_usdt), _decimal(fill.base_qty_after),
                        _decimal(fill.reference_price_usdt), _decimal(fill.slippage_cost_usdt),
                    ),
                )
                connection.execute(
                    "UPDATE paper_replay_signals SET status='FILLED' WHERE signal_id=?",
                    (signal["signal_id"],),
                )
                row = _load_row(connection, replay_id)
            if ReplayStatus(_load_row(connection, replay_id)["status"]) is ReplayStatus.HALTED:
                break

            current_equity = mark_equity(state, candle.close)
            high_water = max(high_water, current_equity)
            drawdown = (high_water - current_equity) / high_water * Decimal("100") if high_water else Decimal("0")
            max_drawdown = max(max_drawdown, drawdown)
            connection.execute(
                """INSERT INTO paper_replay_equity
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    replay_id, index, _iso(candle.end_at_utc), _decimal(state.cash_usdt),
                    _decimal(state.base_qty), _decimal(candle.close), _decimal(current_equity),
                    _decimal(state.fees_paid_usdt), _decimal(high_water), _decimal(drawdown),
                ),
            )
            processed_index = index
            if drawdown >= _limits_from_dict(json.loads(row["risk_limits_json"])).max_drawdown_pct:
                _halt_tx(connection, replay_id, "MAX_SIMULATION_DRAWDOWN_REACHED", {
                    "drawdown_pct": _decimal(drawdown),
                    "limit_pct": _limits_from_dict(json.loads(row["risk_limits_json"])).max_drawdown_pct.__str__(),
                    "new_fills_blocked": True,
                })
            if ReplayStatus(_load_row(connection, replay_id)["status"]) is ReplayStatus.HALTED:
                break

            row = _load_row(connection, replay_id)
            if index == 0 and config.strategy in {StrategyName.CASH, StrategyName.BUY_HOLD}:
                delay = timedelta(microseconds=int(config.assumed_data_delay_seconds * Decimal("1000000")))
                available = candle.open_at_utc + delay
                _new_signal(
                    connection,
                    replay_id,
                    candle.open_at_utc,
                    available,
                    _strictly_next_minute_open(available),
                    Decimal("1") if config.strategy is StrategyName.BUY_HOLD else Decimal("0"),
                    "baseline compra uma vez na primeira execução elegível e mantém" if config.strategy is StrategyName.BUY_HOLD else "baseline mantém todo o saldo em caixa, sem juros",
                    candle.open_at_utc,
                )
            elif config.strategy in {StrategyName.SMA_CROSS, StrategyName.BREAKOUT}:
                duplicate_decision = connection.execute(
                    "SELECT 1 FROM paper_replay_decisions WHERE replay_id=? AND decision_at_utc=?",
                    (replay_id, _iso(candle.end_at_utc)),
                ).fetchone()
                if duplicate_decision:
                    _halt_tx(connection, replay_id, "DUPLICATE_EVENT_ID", {
                        "event_id": f"{replay_id}:decision:{_iso(candle.end_at_utc)}",
                    })
                    break
                _store_decision_and_signal(connection, row, snapshot, config, index, candle)

        # Persist the last processed bar and ledger together with the journal.
        last_candle = snapshot.candles_1m[processed_index] if processed_index >= 0 else None
        current_row = _load_row(connection, replay_id)
        connection.execute(
            """UPDATE paper_replays SET processed_index=?,simulated_at_utc=?,cash_usdt=?,base_qty=?,
               fees_paid_usdt=?,current_equity_usdt=?,high_water_usdt=?,max_drawdown_pct=?,updated_at_utc=?
               WHERE replay_id=?""",
            (
                processed_index,
                _iso(last_candle.end_at_utc) if last_candle else None,
                _decimal(state.cash_usdt),
                _decimal(state.base_qty),
                _decimal(state.fees_paid_usdt),
                _decimal(current_equity),
                _decimal(high_water),
                _decimal(max_drawdown),
                _iso(datetime.now(UTC)),
                replay_id,
            ),
        )
        last_candle = snapshot.candles_1m[processed_index] if processed_index >= 0 else None
        checkpoint_at = last_candle.end_at_utc if last_candle else datetime.now(UTC)
        _event_at(connection, replay_id, "CHECKPOINT", checkpoint_at, {
            "processed_index": processed_index,
            "simulated_at_utc": _iso(checkpoint_at) if last_candle else None,
            "cash_usdt": _decimal(state.cash_usdt),
            "base_qty": _decimal(state.base_qty),
            "fees_paid_usdt": _decimal(state.fees_paid_usdt),
            "equity_usdt": _decimal(current_equity),
            "config_sha256": current_row["config_sha256"],
            "snapshot_sha256": current_row["snapshot_sha256"],
        })

        current_row = _load_row(connection, replay_id)
        if (
            ReplayStatus(current_row["status"]) is ReplayStatus.RUNNING
            and processed_index == len(snapshot.candles_1m) - 1
        ):
            pending_rows = connection.execute(
                "SELECT * FROM paper_replay_signals WHERE replay_id=? AND status='PENDING'",
                (replay_id,),
            ).fetchall()
            timeframe_width = timedelta(minutes=TIMEFRAME_MINUTES[config.decision_timeframe])
            epoch = datetime(1970, 1, 1, tzinfo=UTC)
            incomplete_tail = bool(snapshot.candles_1m) and (
                (snapshot.candles_1m[-1].end_at_utc - epoch) % timeframe_width != timedelta(0)
            )
            if pending_rows:
                for signal in pending_rows:
                    connection.execute(
                        "UPDATE paper_replay_signals SET status='VETOED_SNAPSHOT_END',veto_reason='SNAPSHOT_END_BEFORE_ELIGIBLE_BAR' WHERE signal_id=?",
                        (signal["signal_id"],),
                    )
                    _event_at(connection, replay_id, "RISK_VETO", _parse_time(signal["eligible_at_utc"]), {
                        "signal_id": signal["signal_id"],
                        "reason": "SNAPSHOT_END_BEFORE_ELIGIBLE_BAR",
                        "new_fill_created": False,
                    })
                _transition_tx(connection, replay_id, ReplayStatus.DEGRADED, "SNAPSHOT_END_BEFORE_ELIGIBLE_FILL", {
                    "pending_signals": len(pending_rows),
                })
            elif incomplete_tail:
                _transition_tx(connection, replay_id, ReplayStatus.DEGRADED, "INCOMPLETE_FINAL_DECISION_WINDOW", {
                    "timeframe": config.decision_timeframe,
                    "last_bar_end_at_utc": _iso(snapshot.candles_1m[-1].end_at_utc),
                })
            else:
                _transition_tx(connection, replay_id, ReplayStatus.STOPPED, "REPLAY_COMPLETED_AT_SNAPSHOT_END")
    return get_paper_replay(root, replay_id)


def _fill_payload(
    fill: SimulatedFill,
    signal_id: str,
    bar_index: int,
    target_fraction: Decimal,
) -> dict[str, Any]:
    return {
        "signal_id": signal_id,
        "bar_index": bar_index,
        "timestamp_utc": _iso(fill.timestamp_utc),
        "target_fraction": _decimal(target_fraction),
        "side": fill.side.value,
        "quantity_base": _decimal(fill.quantity_base),
        "price_usdt": _decimal(fill.price_usdt),
        "quote_amount_usdt": _decimal(fill.quote_amount_usdt),
        "fee_usdt": _decimal(fill.fee_usdt),
        "cash_after_usdt": _decimal(fill.cash_after_usdt),
        "base_qty_after": _decimal(fill.base_qty_after),
        "reference_price_usdt": _decimal(fill.reference_price_usdt),
        "slippage_cost_usdt": _decimal(fill.slippage_cost_usdt),
    }


def compare_paper_replay_to_backtest(
    root: Path,
    replay_id: str,
    snapshot: BacktestSnapshot,
) -> dict[str, Any]:
    session = get_paper_replay(root, replay_id)
    if session.processed_index < 0 or session.simulated_at_utc is None:
        raise ReplayError("Avance ao menos um candle antes da comparação.")
    check = reconcile_paper_replay(root, replay_id, snapshot)
    if not check["ok"]:
        raise ReplayError(f"Replay HALTED durante reconciliação: {check['reason']}")
    result = run_backtest_window(
        snapshot,
        session.config,
        start_at_utc=snapshot.candles_1m[0].open_at_utc,
        end_at_utc=session.simulated_at_utc,
        evidence_level="PAPER_REPLAY",
    )
    backtest_result = next(item for item in result.results if item.strategy is session.config.strategy)
    with _db(root) as connection:
        replay_fills = connection.execute(
            "SELECT * FROM paper_replay_fills WHERE replay_id=? ORDER BY bar_index,timestamp_utc,event_id",
            (replay_id,),
        ).fetchall()
        replay_equity = connection.execute(
            "SELECT * FROM paper_replay_equity WHERE replay_id=? ORDER BY bar_index", (replay_id,)
        ).fetchall()
    replay_fill_values = [
        (row["timestamp_utc"], row["side"], row["quantity_base"], row["price_usdt"],
         row["quote_amount_usdt"], row["fee_usdt"], row["cash_after_usdt"], row["base_qty_after"],
         row["reference_price_usdt"], row["slippage_cost_usdt"])
        for row in replay_fills
    ]
    backtest_fill_values = [
        (_iso(fill.timestamp_utc), fill.side.value, _decimal(fill.quantity_base), _decimal(fill.price_usdt),
         _decimal(fill.quote_amount_usdt), _decimal(fill.fee_usdt), _decimal(fill.cash_after_usdt),
         _decimal(fill.base_qty_after), _decimal(fill.reference_price_usdt), _decimal(fill.slippage_cost_usdt))
        for fill in backtest_result.fills
    ]
    replay_equity_values = [
        (row["timestamp_utc"], row["cash_usdt"], row["base_qty"], row["mark_price_usdt"],
         row["equity_usdt"], row["fees_paid_usdt"])
        for row in replay_equity
    ]
    backtest_equity_values = [
        (_iso(point.timestamp_utc), _decimal(point.cash_usdt), _decimal(point.base_qty),
         _decimal(point.mark_price_usdt), _decimal(point.equity_usdt), _decimal(point.fees_paid_usdt))
        for point in backtest_result.equity
    ]
    differences: list[dict[str, Any]] = []
    if replay_fill_values != backtest_fill_values:
        differences.append({
            "field": "fills",
            "replay_count": len(replay_fill_values),
            "backtest_count": len(backtest_fill_values),
            "first_replay_difference": _first_difference(replay_fill_values, backtest_fill_values),
        })
    if replay_equity_values != backtest_equity_values:
        differences.append({
            "field": "equity",
            "replay_count": len(replay_equity_values),
            "backtest_count": len(backtest_equity_values),
            "first_replay_difference": _first_difference(replay_equity_values, backtest_equity_values),
        })
    metrics = backtest_result.metrics
    comparison = {
        "comparison_id": f"comparison-{uuid4().hex}",
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_sha256": snapshot.content_sha256,
        "config_sha256": session.config_sha256,
        "window_start_at_utc": _iso(snapshot.candles_1m[0].open_at_utc),
        "window_end_at_utc_exclusive": _iso(session.simulated_at_utc),
        "evidence_level": "PAPER_REPLAY",
        "equal": not differences,
        "differences": differences,
        "policy_differences": [
            "F7 aplica RiskGate de exposição e drawdown; F3 não aplica esses limites.",
            "F7 interrompe em gap 1m e não avança fill; F3 registra veto para eventos afetados e continua o backtest.",
            "Evidência limitada a replay histórico offline (PAPER_REPLAY).",
        ],
        "replay": {
            "fill_count": len(replay_fill_values),
            "equity_points": len(replay_equity_values),
            "cash_usdt": _decimal(session.cash_usdt),
            "base_qty": _decimal(session.base_qty),
            "fees_paid_usdt": _decimal(session.fees_paid_usdt),
            "equity_usdt": _decimal(session.current_equity_usdt),
        },
        "backtest": {
            "fill_count": len(backtest_fill_values),
            "equity_points": len(backtest_equity_values),
            "cash_usdt": _decimal(metrics.final_cash_usdt),
            "base_qty": _decimal(metrics.final_base_qty),
            "fees_paid_usdt": _decimal(metrics.total_fees_usdt),
            "equity_usdt": _decimal(metrics.final_equity_usdt),
        },
    }
    with _db(root) as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = _load_row(connection, replay_id)
        _append_event(connection, replay_id, "BACKTEST_COMPARISON", session.simulated_at_utc, comparison)
        connection.execute(
            "UPDATE paper_replays SET comparison_json=?,compared_at_utc=?,updated_at_utc=? WHERE replay_id=?",
            (_canonical(comparison), _iso(datetime.now(UTC)), _iso(datetime.now(UTC)), replay_id),
        )
    return comparison


def _first_difference(left: list[tuple[Any, ...]], right: list[tuple[Any, ...]]) -> dict[str, Any] | None:
    for index, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return {"index": index, "replay": list(a), "backtest": list(b)}
    if len(left) != len(right):
        return {"index": min(len(left), len(right)), "replay": None if len(left) <= len(right) else list(left[len(right)]),
                "backtest": None if len(right) <= len(left) else list(right[len(left)])}
    return None


def export_paper_replay_journal(root: Path, replay_id: str) -> dict[str, Any]:
    with _db(root) as connection:
        row = _load_row(connection, replay_id)
        events = connection.execute(
            "SELECT event_id,sequence,event_type,occurred_at_utc,payload_json,previous_hash,event_hash FROM paper_replay_events WHERE replay_id=? ORDER BY sequence",
            (replay_id,),
        ).fetchall()
        signals = connection.execute(
            "SELECT * FROM paper_replay_signals WHERE replay_id=? ORDER BY generated_at_utc,signal_id", (replay_id,)
        ).fetchall()
        fills = connection.execute(
            "SELECT * FROM paper_replay_fills WHERE replay_id=? ORDER BY bar_index,timestamp_utc", (replay_id,)
        ).fetchall()
        equity = connection.execute(
            "SELECT * FROM paper_replay_equity WHERE replay_id=? ORDER BY bar_index", (replay_id,)
        ).fetchall()
        incidents = connection.execute(
            "SELECT detected_at_utc,reason,details_json FROM paper_replay_integrity_incidents WHERE replay_id=? ORDER BY detected_at_utc",
            (replay_id,),
        ).fetchall()
        return {
            "artifact_type": "PAPER_REPLAY_EVENT_JOURNAL",
            "evidence_level": "PAPER_REPLAY",
            "replay": {
                "replay_id": row["replay_id"],
                "snapshot_id": row["snapshot_id"],
                "snapshot_sha256": row["snapshot_sha256"],
                "data_origin": row["data_origin"],
                "config": json.loads(row["config_json"]),
                "config_sha256": row["config_sha256"],
                "risk_limits": json.loads(row["risk_limits_json"]),
                "status": row["status"],
                "state_reason": row["state_reason"],
                "processed_index": row["processed_index"],
                "simulated_at_utc": row["simulated_at_utc"],
                "cash_usdt": row["cash_usdt"],
                "base_qty": row["base_qty"],
                "fees_paid_usdt": row["fees_paid_usdt"],
                "current_equity_usdt": row["current_equity_usdt"],
                "high_water_usdt": row["high_water_usdt"],
                "max_drawdown_pct": row["max_drawdown_pct"],
                "event_chain_sha256": row["event_chain_sha256"],
                "parent_replay_id": row["parent_replay_id"],
                "comparison": json.loads(row["comparison_json"]) if row["comparison_json"] else None,
            },
            "events": [
                {
                    "event_id": item["event_id"],
                    "sequence": item["sequence"],
                    "event_type": item["event_type"],
                    "occurred_at_utc": item["occurred_at_utc"],
                    "payload": json.loads(item["payload_json"]),
                    "previous_hash": item["previous_hash"],
                    "event_hash": item["event_hash"],
                }
                for item in events
            ],
            "signals": [dict(item) for item in signals],
            "fills": [dict(item) for item in fills],
            "equity": [dict(item) for item in equity],
            "integrity_incidents": [
                {"detected_at_utc": item["detected_at_utc"], "reason": item["reason"], "details": json.loads(item["details_json"])}
                for item in incidents
            ],
        }


def list_paper_replay_events(
    root: Path,
    replay_id: str,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if not 1 <= limit <= 500:
        raise ValueError("limit deve estar entre 1 e 500")
    with _db(root) as connection:
        _load_row(connection, replay_id)
        rows = connection.execute(
            """SELECT event_id,sequence,event_type,occurred_at_utc,payload_json,event_hash
               FROM paper_replay_events WHERE replay_id=? ORDER BY sequence DESC LIMIT ?""",
            (replay_id, limit),
        ).fetchall()
    return [
        {
            "event_id": item["event_id"],
            "sequence": item["sequence"],
            "event_type": item["event_type"],
            "occurred_at_utc": item["occurred_at_utc"],
            "payload": json.loads(item["payload_json"]),
            "event_hash": item["event_hash"],
        }
        for item in reversed(rows)
    ]


def export_equity_csv(root: Path, replay_id: str) -> str:
    journal = export_paper_replay_journal(root, replay_id)
    output = io.StringIO(newline="")
    columns = (
        "bar_index", "timestamp_utc", "cash_usdt", "base_qty", "mark_price_usdt",
        "equity_usdt", "fees_paid_usdt", "high_water_usdt", "drawdown_pct",
    )
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(journal["equity"])
    return output.getvalue()


def export_paper_replay_html(root: Path, replay_id: str) -> str:
    """Build a self-contained local report labelled only as PAPER_REPLAY."""
    journal = export_paper_replay_journal(root, replay_id)
    replay = journal["replay"]
    config = replay["config"]
    risk_limits = replay["risk_limits"]
    comparison = replay.get("comparison")
    event_rows = "\n".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td><code>{}</code></td></tr>".format(
            html.escape(str(item["sequence"])),
            html.escape(str(item["occurred_at_utc"])),
            html.escape(str(item["event_type"])),
            html.escape(_canonical(item["payload"])),
        )
        for item in journal["events"]
    )
    fill_rows = "\n".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(str(item["timestamp_utc"])),
            html.escape(str(item["side"])),
            html.escape(str(item["quantity_base"])),
            html.escape(str(item["price_usdt"])),
            html.escape(str(item["fee_usdt"])),
            html.escape(str(item["cash_after_usdt"])),
        )
        for item in journal["fills"]
    )
    comparison_html = (
        f"<pre>{html.escape(json.dumps(comparison, indent=2, ensure_ascii=False, sort_keys=True))}</pre>"
        if comparison else "<p>A comparação com F3 ainda não foi solicitada.</p>"
    )
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>QuantLab · Paper Replay {html.escape(str(replay['replay_id']))}</title>
  <style>
    :root {{ color-scheme: light; font-family: system-ui, sans-serif; color: #172126; background: #f4f6f4; }}
    body {{ max-width: 1120px; margin: 2rem auto; padding: 0 1rem; }}
    header, section {{ background: white; border: 1px solid #d5dfda; border-radius: 12px; padding: 1.25rem; margin: 1rem 0; }}
    h1, h2 {{ margin-top: 0; }}
    .badge {{ display: inline-block; padding: .35rem .7rem; border-radius: 999px; background: #dcefe7; font-weight: 700; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(210px,1fr)); gap: .8rem; }}
    .metric {{ background: #f2f6f3; padding: .8rem; border-radius: 8px; overflow-wrap: anywhere; }}
    table {{ width: 100%; border-collapse: collapse; font-size: .9rem; }}
    th, td {{ border-bottom: 1px solid #e0e6e2; padding: .5rem; text-align: left; vertical-align: top; }}
    td code {{ overflow-wrap: anywhere; white-space: pre-wrap; }}
    .scroll {{ overflow-x: auto; }}
    pre {{ overflow-x: auto; white-space: pre-wrap; overflow-wrap: anywhere; }}
    .notice {{ color: #43534b; }}
  </style>
</head>
<body>
  <header>
    <span class="badge">PAPER_REPLAY</span>
    <h1>Replay histórico offline</h1>
    <p>Esta evidência descreve uma simulação sequencial de histórico. Não representa uma coleta futura nem execução em mercado.</p>
    <p>Origem dos dados: <strong>{html.escape(str(replay['data_origin']))}</strong> · Sessão: <code>{html.escape(str(replay['replay_id']))}</code> · Estado: <strong>{html.escape(str(replay['status']))}</strong></p>
    <p>Motivo atual: {html.escape(str(replay['state_reason']))} · relógio simulado UTC: {html.escape(str(replay['simulated_at_utc'] or 'ainda não iniciado'))}</p>
  </header>
  <section>
    <h2>Snapshot e estratégia congelada</h2>
    <div class="grid">
      <div class="metric"><strong>Snapshot</strong><br>{html.escape(str(replay['snapshot_id']))}</div>
      <div class="metric"><strong>SHA-256 do snapshot</strong><br><code>{html.escape(str(replay['snapshot_sha256']))}</code></div>
      <div class="metric"><strong>SHA-256 da configuração</strong><br><code>{html.escape(str(replay['config_sha256']))}</code></div>
      <div class="metric"><strong>Regra</strong><br>{html.escape(str(config['strategy']))} · {html.escape(str(config['decision_timeframe']))}</div>
    </div>
    <p>Capital inicial: {html.escape(str(config['initial_cash_usdt']))} USDT · fee: {html.escape(str(config['fee_bps_per_side']))} bps/lado · slippage proxy: {html.escape(str(config['slippage_bps_per_side']))} bps/lado · delay assumido: {html.escape(str(config['assumed_data_delay_seconds']))} s.</p>
    <p>RiskGate congelado: exposição máxima {html.escape(str(Decimal(risk_limits['max_exposure_fraction']) * Decimal('100')))}% · drawdown máximo {html.escape(str(risk_limits['max_drawdown_pct']))}%.</p>
    <p class="notice">Disponibilidade histórica é assumida; fee e slippage são hipóteses; o open OHLCV é uma proxy de fill. Não há ordens reais.</p>
  </section>
  <section>
    <h2>Conciliação no checkpoint</h2>
    <div class="grid">
      <div class="metric"><strong>Caixa</strong><br>{html.escape(str(replay['cash_usdt']))} USDT</div>
      <div class="metric"><strong>Quantidade base</strong><br>{html.escape(str(replay['base_qty']))}</div>
      <div class="metric"><strong>Fees acumuladas</strong><br>{html.escape(str(replay['fees_paid_usdt']))} USDT</div>
      <div class="metric"><strong>Equity marcada</strong><br>{html.escape(str(replay['current_equity_usdt']))} USDT</div>
      <div class="metric"><strong>Drawdown máximo</strong><br>{html.escape(str(replay['max_drawdown_pct']))}%</div>
      <div class="metric"><strong>Hash do journal</strong><br><code>{html.escape(str(replay['event_chain_sha256']))}</code></div>
    </div>
    <p>Equity em cada barra é calculada como caixa + quantidade base × preço de marcação. Use o equity CSV e o journal JSON para auditar todos os pontos e fills.</p>
  </section>
  <section class="scroll">
    <h2>Fills simulados</h2>
    <table><thead><tr><th>UTC</th><th>Lado</th><th>Quantidade base</th><th>Preço</th><th>Fee USDT</th><th>Caixa após fill</th></tr></thead>
    <tbody>{fill_rows or '<tr><td colspan="6">Nenhum fill</td></tr>'}</tbody></table>
  </section>
  <section>
    <h2>Replay vs backtest F3</h2>
    {comparison_html}
  </section>
  <section class="scroll">
    <h2>Journal de eventos</h2>
    <table><thead><tr><th>Seq.</th><th>UTC</th><th>Evento</th><th>Detalhes</th></tr></thead>
    <tbody>{event_rows or '<tr><td colspan="4">Nenhum evento</td></tr>'}</tbody></table>
  </section>
</body>
</html>
"""

