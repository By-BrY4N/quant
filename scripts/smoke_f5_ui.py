"""Smoke offline da UI F5 e do planejamento sem alterar o catálogo do projeto."""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from streamlit.testing.v1 import AppTest

from quantlab.backtest import BacktestConfig, BacktestSnapshot, run_backtest
from quantlab.backtest_artifacts import write_backtest_artifacts
from quantlab.cli import main as cli_main
from quantlab.domain import Candle, StrategyName


def _snapshot(label: str, *, start: datetime) -> BacktestSnapshot:
    instrument = "BINANCE:SPOT:BTCUSDT"
    candles = tuple(
        Candle(
            open_at_utc=start + timedelta(minutes=index),
            end_at_utc=start + timedelta(minutes=index + 1),
            open=Decimal("42000"),
            high=Decimal("42001"),
            low=Decimal("41999"),
            close=Decimal("42000"),
            volume_base=Decimal("0.01"),
            instrument_id=instrument,
        )
        for index in range(240)
    )
    return BacktestSnapshot(
        snapshot_id=label,
        instrument_id=instrument,
        candles_1m=candles,
        content_sha256=hashlib.sha256(label.encode("ascii")).hexdigest(),
        qa_status="PASS",
        data_origin="SYNTHETIC",
    )


def main() -> int:
    project = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="quantlab-f5-smoke-") as temporary:
        try:
            os.chdir(temporary)
            config = BacktestConfig(
                strategy=StrategyName.CASH,
                decision_timeframe="1h",
                initial_cash_usdt=Decimal("1000"),
                fee_bps_per_side=Decimal("0"),
                slippage_bps_per_side=Decimal("0"),
            )
            same_snapshot = _snapshot("smoke-same-snapshot", start=datetime(2024, 2, 1, tzinfo=UTC))
            first_run = write_backtest_artifacts(Path(temporary) / "artifacts" / "backtests", run_backtest(same_snapshot, config))
            second_run = write_backtest_artifacts(Path(temporary) / "artifacts" / "backtests", run_backtest(same_snapshot, config))
            different_snapshot = _snapshot("smoke-other-snapshot", start=datetime(2024, 2, 2, tzinfo=UTC))
            mismatched_run = write_backtest_artifacts(Path(temporary) / "artifacts" / "backtests", run_backtest(different_snapshot, config))

            app = AppTest.from_file(str(project / "src" / "quantlab" / "ui" / "app.py"), default_timeout=30)
            app.run()
            if app.exception:
                raise AssertionError([item.message for item in app.exception])
            pages = ("Dados", "Backtests", "Operações", "Comparar", "Pesquisa", "Runs anteriores")
            for page in pages:
                app.sidebar.radio[0].set_value(page)
                app.run()
                if app.exception:
                    raise AssertionError([item.message for item in app.exception])

            app.sidebar.radio[0].set_value("Comparar")
            app.run()
            app.multiselect[0].set_value([first_run["run_id"], second_run["run_id"]])
            app.run()
            if not any("coincidem" in item.value for item in app.success):
                raise AssertionError("Comparador não reconheceu dois runs compatíveis.")
            app.multiselect[0].set_value([first_run["run_id"], mismatched_run["run_id"]])
            app.run()
            if not any("incompatíveis" in item.value for item in app.error):
                raise AssertionError("Comparador não avisou da diferença de período/snapshot.")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = cli_main([
                    "data", "plan", "--symbol", "ETHUSDT", "--granularity", "daily",
                    "--start", "2024-02-01", "--end", "2024-02-01",
                    "--max-transfer-mb", "2", "--offline", "--root", temporary,
                ])
            plan = output.getvalue()
            if status != 0 or "1 ZIP(s)" not in plan or "2,097,152 bytes" not in plan:
                raise AssertionError(plan)
            print(f"F5_SMOKE_OK pages={','.join(pages)} compare=compatible_and_mismatch daily_plan=1_zip transfer_cap=2_MiB")
        finally:
            os.chdir(project)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
