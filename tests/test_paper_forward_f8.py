from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from quantlab.domain import PortfolioState
from quantlab.paper_forward import ForwardConfig, ForwardEngine


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _closed_kline(opened: datetime, event_at: datetime, *, close: str = "100") -> dict:
    return {
        "stream": "btcusdt@kline_1m",
        "data": {
            "e": "kline", "E": _ms(event_at), "s": "BTCUSDT",
            "k": {
                "t": _ms(opened), "T": _ms(opened + timedelta(minutes=1)) - 1,
                "s": "BTCUSDT", "i": "1m", "o": close, "h": close,
                "l": close, "c": close, "v": "1.25", "x": True,
            },
        },
    }


def _trade(aggregate_id: int, at: datetime, *, price: str = "100") -> dict:
    return {
        "stream": "btcusdt@aggTrade",
        "data": {"e": "aggTrade", "E": _ms(at), "T": _ms(at),
                 "s": "BTCUSDT", "a": aggregate_id, "p": price},
    }


class PaperForwardF8Tests(unittest.TestCase):
    def _engine(self, root: Path, config: ForwardConfig | None = None) -> ForwardEngine:
        active = datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)
        return ForwardEngine(
            config=config or ForwardConfig(), root=root, run_id="PF-test",
            clock_anchor=active, monotonic_anchor=100.0, clock_rtt_seconds=0.1,
            active_at=active,
        )

    def test_only_closed_fully_observed_candle_signals_and_fill_is_strictly_later(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            engine = self._engine(root)
            opened = datetime(2026, 1, 1, 0, 1, tzinfo=UTC)
            signal_receipt = datetime(2026, 1, 1, 0, 2, 0, 500000, tzinfo=UTC)
            signal_event = datetime(2026, 1, 1, 0, 2, 0, 300000, tzinfo=UTC)
            open_message = _closed_kline(opened, signal_event)
            open_message["data"]["k"]["x"] = False
            engine.observe(open_message, receive_time=signal_receipt, monotonic_now=101)
            self.assertIsNone(engine.pending_signal)

            engine.observe(_closed_kline(opened, signal_event), receive_time=signal_receipt, monotonic_now=102)
            self.assertIsNotNone(engine.pending_signal)
            available = engine.pending_signal["available_at"]
            self.assertGreater(available, signal_receipt)
            self.assertEqual(engine.pending_signal["candle_end_at_utc"], "2026-01-01T00:02:00.000Z")

            before_available = datetime(2026, 1, 1, 0, 2, 1, 400000, tzinfo=UTC)
            engine.observe(_trade(10, before_available), receive_time=before_available, monotonic_now=103)
            self.assertEqual(engine.state.base_qty, Decimal("0"))

            after_available = datetime(2026, 1, 1, 0, 2, 2, 100000, tzinfo=UTC)
            engine.observe(_trade(11, after_available), receive_time=after_available, monotonic_now=104)
            self.assertGreater(engine.state.base_qty, 0)
            self.assertGreaterEqual(engine.state.cash_usdt, 0)
            self.assertEqual(engine.state.fees_paid_usdt, engine.state.base_qty * Decimal("100.05") * Decimal("0.001"))
            entries = [json.loads(line) for line in (engine.path / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
            fill = next(item for item in entries if item["kind"] == "SIMULATED_FILL")
            self.assertGreater(datetime.fromisoformat(fill["fill_at_utc"].replace("Z", "+00:00")), available)
            self.assertEqual(fill["venue"], "BINANCE")
            self.assertEqual(fill["instrument_id"], "BINANCE:SPOT:BTCUSDT")

    def test_pre_activation_candle_is_ignored_and_gap_halts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine = self._engine(Path(temporary))
            before = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
            engine.observe(
                _closed_kline(before, before + timedelta(minutes=1)),
                receive_time=before + timedelta(minutes=1), monotonic_now=101,
            )
            self.assertIsNone(engine.pending_signal)
            opened = datetime(2026, 1, 1, 0, 1, tzinfo=UTC)
            engine.observe(
                _closed_kline(opened, opened + timedelta(minutes=1)),
                receive_time=opened + timedelta(minutes=1), monotonic_now=102,
            )
            gap_open = datetime(2026, 1, 1, 0, 3, tzinfo=UTC)
            engine.observe(
                _closed_kline(gap_open, gap_open + timedelta(minutes=1)),
                receive_time=gap_open + timedelta(minutes=1), monotonic_now=103,
            )
            self.assertEqual(engine.status, "HALTED")
            self.assertEqual(engine.reason, "CLOSED_CANDLE_GAP_OR_REORDER")

    def test_stale_heartbeat_halts_and_duplicate_trade_sequence_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine = self._engine(Path(temporary))
            engine.heartbeat(146)
            self.assertEqual(engine.status, "HALTED")
            self.assertEqual(engine.reason, "MARKET_FEED_STALE")

        with tempfile.TemporaryDirectory() as temporary:
            engine = self._engine(Path(temporary))
            at = datetime(2026, 1, 1, 0, 0, 40, tzinfo=UTC)
            engine.observe(_trade(20, at), receive_time=at, monotonic_now=101)
            engine.observe(_trade(22, at + timedelta(seconds=1)),
                           receive_time=at + timedelta(seconds=1), monotonic_now=102)
            self.assertEqual(engine.status, "HALTED")
            self.assertEqual(engine.reason, "AGG_TRADE_SEQUENCE_GAP_OR_DUPLICATE")

    def test_invalid_trade_time_does_not_change_mark_or_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine = self._engine(Path(temporary))
            received = datetime(2026, 1, 1, 0, 0, 40, tzinfo=UTC)
            invalid = _trade(20, received)
            invalid["data"]["T"] = _ms(received - timedelta(seconds=31))
            engine.observe(invalid, receive_time=received, monotonic_now=101)
            self.assertEqual(engine.status, "HALTED")
            self.assertEqual(engine.reason, "STALE_SOURCE_TIMESTAMP")
            self.assertIsNone(engine.last_price)
            self.assertIsNone(engine.last_agg_id)
            self.assertEqual(engine.equity, Decimal("1000"))

    def test_projected_fee_and_slippage_drawdown_prevent_fill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = ForwardConfig(max_drawdown_pct=Decimal("0.01"))
            engine = self._engine(Path(temporary), config)
            opened = datetime(2026, 1, 1, 0, 1, tzinfo=UTC)
            event_at = datetime(2026, 1, 1, 0, 2, 0, 300000, tzinfo=UTC)
            receipt = datetime(2026, 1, 1, 0, 2, 0, 500000, tzinfo=UTC)
            engine.observe(_closed_kline(opened, event_at), receive_time=receipt, monotonic_now=101)
            trade_at = datetime(2026, 1, 1, 0, 2, 2, tzinfo=UTC)
            engine.observe(_trade(30, trade_at), receive_time=trade_at, monotonic_now=102)
            self.assertEqual(engine.status, "HALTED")
            self.assertEqual(engine.reason, "MAX_SIMULATION_DRAWDOWN_REACHED")
            self.assertEqual(engine.state.base_qty, Decimal("0"))
            entries = [json.loads(line) for line in (engine.path / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertFalse(any(item["kind"] == "SIMULATED_FILL" for item in entries))
            halted = next(item for item in entries if item["kind"] == "HALTED")
            self.assertTrue(halted["fill_prevented"])
            self.assertGreater(Decimal(halted["drawdown_pct"]), config.max_drawdown_pct)

    def test_f3_ledger_fee_rejects_negative_wallet_before_forward_session(self) -> None:
        with self.assertRaises(ValueError):
            PortfolioState(Decimal("-1"), Decimal("0"))


if __name__ == "__main__":
    unittest.main()
