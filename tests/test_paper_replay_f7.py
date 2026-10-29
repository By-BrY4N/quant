from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from quantlab.backtest import BacktestConfig, BacktestSnapshot
from quantlab.domain import Candle, PortfolioState, StrategyName
from quantlab.paper_replay import (
    ReplayStatus,
    RiskLimits,
    _db,
    _new_signal,
    advance_paper_replay,
    create_paper_replay,
    get_paper_replay,
    pause_paper_replay,
    reconcile_paper_replay,
    resume_paper_replay,
    _mark_opening_risk,
)


def _snapshot(minutes: int, *, missing_index: int | None = None) -> BacktestSnapshot:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    candles: list[Candle] = []
    for index in range(minutes):
        if index == missing_index:
            continue
        price = Decimal("100") + Decimal(index) / Decimal("100")
        candles.append(Candle(
            open_at_utc=start + timedelta(minutes=index),
            end_at_utc=start + timedelta(minutes=index + 1),
            open=price,
            high=price + Decimal("0.1"),
            low=price - Decimal("0.1"),
            close=price,
            volume_base=Decimal("1"),
            instrument_id="BINANCE:SPOT:BTCUSDT",
        ))
    content = "\n".join(
        "|".join(str(value) for value in (
            candle.open_at_utc.isoformat(), candle.open, candle.high, candle.low, candle.close, candle.volume_base,
        ))
        for candle in candles
    ).encode()
    return BacktestSnapshot(
        snapshot_id=f"f7-test-{minutes}-{missing_index}",
        instrument_id="BINANCE:SPOT:BTCUSDT",
        candles_1m=tuple(candles),
        content_sha256=hashlib.sha256(content).hexdigest(),
        qa_status="PASS",
        data_origin="SYNTHETIC",
    )


class PaperReplayF7Tests(unittest.TestCase):
    def test_opening_gap_drawdown_is_measured_before_approving_execution(self) -> None:
        state = PortfolioState(Decimal("0"), Decimal("1"))
        opening_equity, opening_drawdown = _mark_opening_risk(
            state, Decimal("70"), Decimal("100"), Decimal("0"),
        )
        self.assertEqual(opening_equity, Decimal("70"))
        self.assertEqual(opening_drawdown, Decimal("30"))

    def test_restart_reconciles_once_and_duplicate_signal_id_halts_without_second_fill(self) -> None:
        snapshot = _snapshot(180)
        config = BacktestConfig(
            strategy=StrategyName.BUY_HOLD,
            initial_cash_usdt=Decimal("1000"),
            fee_bps_per_side=Decimal("10"),
            slippage_bps_per_side=Decimal("5"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = create_paper_replay(root, snapshot, config, RiskLimits())
            advance_paper_replay(root, session.replay_id, snapshot, max_candles=3)
            paused = pause_paper_replay(root, session.replay_id)
            self.assertEqual(paused.status, ReplayStatus.READY)

            after_restart = reconcile_paper_replay(root, session.replay_id, snapshot)
            self.assertTrue(after_restart["ok"], after_restart)
            self.assertEqual(after_restart["replayed_fills"], 1)
            resumed = resume_paper_replay(root, session.replay_id, snapshot)
            self.assertEqual(resumed.status, ReplayStatus.RUNNING)
            advance_paper_replay(root, session.replay_id, snapshot, max_candles=5)

            with _db(root) as connection:
                signal = connection.execute(
                    "SELECT * FROM paper_replay_signals WHERE replay_id=?",
                    (session.replay_id,),
                ).fetchone()
                fill_count = connection.execute(
                    "SELECT count(*) FROM paper_replay_fills WHERE replay_id=?",
                    (session.replay_id,),
                ).fetchone()[0]
                fees_before_duplicate = connection.execute(
                    "SELECT coalesce(sum(cast(fee_usdt AS REAL)),0) FROM paper_replay_fills WHERE replay_id=?",
                    (session.replay_id,),
                ).fetchone()[0]
                connection.execute("BEGIN IMMEDIATE")
                duplicate_accepted = _new_signal(
                    connection,
                    session.replay_id,
                    datetime.fromisoformat(signal["generated_at_utc"].replace("Z", "+00:00")),
                    datetime.fromisoformat(signal["available_at_utc"].replace("Z", "+00:00")),
                    datetime.fromisoformat(signal["eligible_at_utc"].replace("Z", "+00:00")),
                    Decimal(signal["target_fraction"]),
                    signal["reason"],
                    datetime.fromisoformat(signal["decision_at_utc"].replace("Z", "+00:00")),
                )
                self.assertFalse(duplicate_accepted)

            halted = get_paper_replay(root, session.replay_id)
            self.assertEqual(halted.status, ReplayStatus.HALTED)
            self.assertEqual(halted.state_reason, "DUPLICATE_EVENT_ID")
            with _db(root) as connection:
                fill_count_after = connection.execute(
                    "SELECT count(*) FROM paper_replay_fills WHERE replay_id=?",
                    (session.replay_id,),
                ).fetchone()[0]
                fees_after_duplicate = connection.execute(
                    "SELECT coalesce(sum(cast(fee_usdt AS REAL)),0) FROM paper_replay_fills WHERE replay_id=?",
                    (session.replay_id,),
                ).fetchone()[0]
            self.assertEqual(fill_count, 1)
            self.assertEqual(fill_count_after, fill_count)
            self.assertEqual(fees_after_duplicate, fees_before_duplicate)

    def test_strategy_signal_does_not_depend_on_bars_after_simulated_clock(self) -> None:
        original = _snapshot(180)
        altered_candles = list(original.candles_1m)
        for index in range(120, len(altered_candles)):
            price = Decimal("50") + Decimal(index - 120) / Decimal("100")
            altered_candles[index] = Candle(
                open_at_utc=altered_candles[index].open_at_utc,
                end_at_utc=altered_candles[index].end_at_utc,
                open=price,
                high=price + Decimal("0.1"),
                low=price - Decimal("0.1"),
                close=price,
                volume_base=Decimal("1"),
                instrument_id="BINANCE:SPOT:BTCUSDT",
            )
        changed_content = "\n".join(
            "|".join(str(value) for value in (
                item.open_at_utc.isoformat(), item.open, item.high, item.low, item.close, item.volume_base,
            ))
            for item in altered_candles
        ).encode()
        future_changed = BacktestSnapshot(
            snapshot_id="f7-test-future-changed",
            instrument_id=original.instrument_id,
            candles_1m=tuple(altered_candles),
            content_sha256=hashlib.sha256(changed_content).hexdigest(),
            qa_status="PASS",
            data_origin="SYNTHETIC",
        )
        config = BacktestConfig(
            strategy=StrategyName.SMA_CROSS,
            decision_timeframe="1h",
            sma_fast=1,
            sma_slow=2,
        )
        with tempfile.TemporaryDirectory() as temporary:
            roots = [Path(temporary) / "original", Path(temporary) / "future-changed"]
            snapshots = [original, future_changed]
            signals = []
            for root, snapshot in zip(roots, snapshots, strict=True):
                replay = create_paper_replay(root, snapshot, config)
                advance_paper_replay(root, replay.replay_id, snapshot, max_candles=120)
                with _db(root) as connection:
                    row = connection.execute(
                        "SELECT target_fraction,reason,generated_at_utc FROM paper_replay_signals WHERE replay_id=? ORDER BY generated_at_utc DESC LIMIT 1",
                        (replay.replay_id,),
                    ).fetchone()
                signals.append(tuple(row))
            self.assertEqual(signals[0], signals[1])
            self.assertEqual(signals[0][2], "2026-01-01T02:00:00Z")

    def test_exposure_risk_gate_vetoes_open_without_fill(self) -> None:
        snapshot = _snapshot(180)
        config = BacktestConfig(strategy=StrategyName.BUY_HOLD)
        limits = RiskLimits(max_exposure_fraction=Decimal("0.5"))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = create_paper_replay(root, snapshot, config, limits)
            advance_paper_replay(root, session.replay_id, snapshot, max_candles=3)
            with _db(root) as connection:
                fill_count = connection.execute(
                    "SELECT count(*) FROM paper_replay_fills WHERE replay_id=?",
                    (session.replay_id,),
                ).fetchone()[0]
                signal = connection.execute(
                    "SELECT status,veto_reason FROM paper_replay_signals WHERE replay_id=?",
                    (session.replay_id,),
                ).fetchone()
            self.assertEqual(fill_count, 0)
            self.assertEqual(signal["status"], "VETOED_MAX_EXPOSURE_LIMIT")
            self.assertEqual(signal["veto_reason"], "MAX_EXPOSURE_LIMIT")

    def test_gap_halts_before_processing_the_next_observed_bar(self) -> None:
        snapshot = _snapshot(150, missing_index=75)
        config = BacktestConfig(strategy=StrategyName.BUY_HOLD)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = create_paper_replay(root, snapshot, config)
            halted = advance_paper_replay(root, session.replay_id, snapshot, max_candles=100)
            self.assertEqual(halted.status, ReplayStatus.HALTED)
            self.assertEqual(halted.state_reason, "GAP_DETECTED")
            self.assertEqual(halted.processed_index, 74)
            with _db(root) as connection:
                later_fills = connection.execute(
                    "SELECT count(*) FROM paper_replay_fills WHERE replay_id=? AND bar_index>74",
                    (session.replay_id,),
                ).fetchone()[0]
            self.assertEqual(later_fills, 0)


if __name__ == "__main__":
    unittest.main()
