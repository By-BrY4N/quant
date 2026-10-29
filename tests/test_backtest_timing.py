"""Check temporalidade causal do agendador F3 e invariância ao futuro."""

from __future__ import annotations

import hashlib
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from quantlab.backtest import (
    BacktestConfig,
    BacktestSnapshot,
    closed_trade_pnl_by_exit_timestamp,
    prepare_backtest,
    run_backtest,
    run_backtest_window,
)
from quantlab.domain import Candle, PortfolioState, Side, StrategyName
from quantlab.ledger import apply_fill


def _minute_fixture(hours: int) -> tuple[Candle, ...]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    candles = []
    for index in range(hours * 60):
        opened = start + timedelta(minutes=index)
        price = Decimal("110") if opened >= datetime(2024, 1, 1, 14, 59, tzinfo=UTC) else Decimal("100")
        candles.append(
            Candle(
                open_at_utc=opened,
                end_at_utc=opened + timedelta(minutes=1),
                open=price,
                high=price,
                low=price,
                close=price,
                volume_base=Decimal("1"),
                instrument_id="BINANCE:SPOT:BTCUSDT",
            )
        )
    return tuple(candles)


def _snapshot(candles: tuple[Candle, ...], label: str) -> BacktestSnapshot:
    digest = hashlib.sha256(label.encode("ascii")).hexdigest()
    return BacktestSnapshot(
        snapshot_id=f"test-{label}",
        instrument_id="BINANCE:SPOT:BTCUSDT",
        candles_1m=candles,
        content_sha256=digest,
        qa_status="PASS",
        data_origin="SYNTHETIC",
    )


class BacktestCausalityTest(unittest.TestCase):
    def test_window_can_skip_unused_baselines_without_changing_candidate_metrics(self) -> None:
        snapshot = _snapshot(_minute_fixture(16), "window-baseline-reuse")
        config = BacktestConfig(
            strategy=StrategyName.SMA_CROSS,
            decision_timeframe="1h",
            fee_bps_per_side=Decimal("10"),
            slippage_bps_per_side=Decimal("5"),
            sma_fast=1,
            sma_slow=2,
        )
        start = datetime(2024, 1, 1, 4, 0, tzinfo=UTC)
        end = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)

        complete = run_backtest_window(
            snapshot, config, start_at_utc=start, end_at_utc=end,
        )
        prepared = prepare_backtest(snapshot, config)
        full_run = run_backtest(snapshot, config)
        prepared_full_run = run_backtest(snapshot, config, prepared=prepared)
        candidate_and_benchmark = run_backtest_window(
            snapshot, config, start_at_utc=start, end_at_utc=end,
            include_cash=False,
        )
        candidate_only = run_backtest_window(
            snapshot, config, start_at_utc=start, end_at_utc=end,
            include_cash=False, include_buy_hold=False,
        )
        prepared_candidate_only = run_backtest_window(
            snapshot, config, start_at_utc=start, end_at_utc=end,
            include_cash=False, include_buy_hold=False, prepared=prepared,
        )

        complete_results = {item.strategy: item for item in complete.results}
        benchmark_results = {item.strategy: item for item in candidate_and_benchmark.results}
        candidate_result = next(item for item in candidate_only.results if item.strategy == config.strategy)
        prepared_candidate_result = next(
            item for item in prepared_candidate_only.results if item.strategy == config.strategy
        )
        self.assertEqual(
            set(benchmark_results),
            {StrategyName.SMA_CROSS, StrategyName.BUY_HOLD},
        )
        self.assertEqual(set(candidate_only.audit["vetoes_by_strategy"]), {"SMA_CROSS"})
        self.assertEqual(
            complete_results[StrategyName.SMA_CROSS].metrics,
            candidate_result.metrics,
        )
        self.assertEqual(
            complete_results[StrategyName.BUY_HOLD].metrics,
            benchmark_results[StrategyName.BUY_HOLD].metrics,
        )
        self.assertEqual(candidate_result.metrics, prepared_candidate_result.metrics)
        self.assertEqual(candidate_result.signals, prepared_candidate_result.signals)
        self.assertEqual(candidate_result.fills, prepared_candidate_result.fills)
        self.assertEqual(candidate_result.equity, prepared_candidate_result.equity)
        full_results = {item.strategy: item for item in full_run.results}
        prepared_full_results = {item.strategy: item for item in prepared_full_run.results}
        self.assertEqual(set(full_results), set(prepared_full_results))
        for strategy, expected in full_results.items():
            actual = prepared_full_results[strategy]
            self.assertEqual(expected.metrics, actual.metrics)
            self.assertEqual(expected.signals, actual.signals)
            self.assertEqual(expected.fills, actual.fills)
            self.assertEqual(expected.equity, actual.equity)

        changed_signal_config = BacktestConfig(
            strategy=StrategyName.SMA_CROSS,
            decision_timeframe="1h",
            sma_fast=1,
            sma_slow=3,
        )
        with self.assertRaisesRegex(ValueError, "outra configuração"):
            run_backtest_window(
                snapshot, changed_signal_config, start_at_utc=start, end_at_utc=end,
                prepared=prepared,
            )

    def test_closed_trade_pnl_includes_entry_and_exit_fees_once(self) -> None:
        state = PortfolioState(cash_usdt=Decimal("1000"), base_qty=Decimal("0"))
        state, buy = apply_fill(
            state,
            timestamp_utc=datetime(2024, 1, 1, tzinfo=UTC),
            side=Side.BUY,
            quantity_base=Decimal("1"),
            price_usdt=Decimal("100"),
            fee_rate=Decimal("0.001"),
        )
        state, sell = apply_fill(
            state,
            timestamp_utc=datetime(2024, 1, 2, tzinfo=UTC),
            side=Side.SELL,
            quantity_base=Decimal("1"),
            price_usdt=Decimal("110"),
            fee_rate=Decimal("0.001"),
        )

        realized = closed_trade_pnl_by_exit_timestamp((buy, sell))

        self.assertEqual(realized, {sell.timestamp_utc: Decimal("9.79")})
        self.assertEqual(state.cash_usdt - Decimal("1000"), realized[sell.timestamp_utc])

    def test_fill_is_strictly_after_available_and_future_does_not_rewrite_history(self) -> None:
        config = BacktestConfig(
            strategy=StrategyName.SMA_CROSS,
            decision_timeframe="1h",
            fee_bps_per_side=Decimal("0"),
            slippage_bps_per_side=Decimal("0"),
            sma_fast=1,
            sma_slow=2,
        )
        historical_candles = _minute_fixture(16)
        extended_candles = _minute_fixture(20)
        historical = run_backtest(_snapshot(historical_candles, "historical"), config)
        extended = run_backtest(_snapshot(extended_candles, "extended"), config)

        historical_result = next(
            item for item in historical.results if item.strategy is StrategyName.SMA_CROSS
        )
        extended_result = next(
            item for item in extended.results if item.strategy is StrategyName.SMA_CROSS
        )
        boundary = datetime(2024, 1, 1, 16, 0, tzinfo=UTC)
        historical_prefix = [
            (event.generated_at_utc, event.available_at_utc, event.eligible_at_utc,
             event.target_exposure, event.reason)
            for event in historical_result.signals
            if event.generated_at_utc <= boundary
        ]
        extended_prefix = [
            (event.generated_at_utc, event.available_at_utc, event.eligible_at_utc,
             event.target_exposure, event.reason)
            for event in extended_result.signals
            if event.generated_at_utc <= boundary
        ]
        self.assertEqual(historical_prefix, extended_prefix)
        historical_fill_prefix = [
            (fill.timestamp_utc, fill.side, fill.quantity_base, fill.price_usdt,
             fill.fee_usdt, fill.cash_after_usdt, fill.base_qty_after)
            for fill in historical_result.fills
            if fill.timestamp_utc <= boundary
        ]
        extended_fill_prefix = [
            (fill.timestamp_utc, fill.side, fill.quantity_base, fill.price_usdt,
             fill.fee_usdt, fill.cash_after_usdt, fill.base_qty_after)
            for fill in extended_result.fills
            if fill.timestamp_utc <= boundary
        ]
        self.assertEqual(historical_fill_prefix, extended_fill_prefix)
        historical_equity_prefix = [
            (point.timestamp_utc, point.cash_usdt, point.base_qty,
             point.mark_price_usdt, point.equity_usdt)
            for point in historical_result.equity
            if point.timestamp_utc <= boundary
        ]
        extended_equity_prefix = [
            (point.timestamp_utc, point.cash_usdt, point.base_qty,
             point.mark_price_usdt, point.equity_usdt)
            for point in extended_result.equity
            if point.timestamp_utc <= boundary
        ]
        self.assertEqual(historical_equity_prefix, extended_equity_prefix)

        signal = next(
            event
            for event in historical_result.signals
            if event.generated_at_utc == datetime(2024, 1, 1, 15, 0, tzinfo=UTC)
        )
        self.assertEqual(signal.available_at_utc, datetime(2024, 1, 1, 15, 0, 1, tzinfo=UTC))
        self.assertEqual(signal.eligible_at_utc, datetime(2024, 1, 1, 15, 1, tzinfo=UTC))
        self.assertGreater(signal.eligible_at_utc, signal.available_at_utc)
        self.assertEqual(signal.execution_at_utc, signal.eligible_at_utc)
        self.assertNotIn(
            datetime(2024, 1, 1, 15, 0, tzinfo=UTC),
            [fill.timestamp_utc for fill in historical_result.fills],
        )
        self.assertIn(
            datetime(2024, 1, 1, 15, 1, tzinfo=UTC),
            [fill.timestamp_utc for fill in historical_result.fills],
        )
        self.assertTrue(
            all(
                event.execution_at_utc is None
                for event in historical_result.signals
                if event.execution_status != "FILLED"
            )
        )


if __name__ == "__main__":
    unittest.main()
