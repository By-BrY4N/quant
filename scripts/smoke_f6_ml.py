from __future__ import annotations

import hashlib
import math
import os
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from streamlit.testing.v1 import AppTest

from quantlab.backtest import BacktestSnapshot
from quantlab.domain import Candle
from quantlab.ml import run_ml_experiment_on_snapshot


def synthetic_snapshot(hours: int = 200) -> BacktestSnapshot:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    instrument = "SYNTH:SPOT:F6SMOKEUSDT"
    candles: list[Candle] = []
    previous_close = Decimal("100")
    for minute_index in range(hours * 60):
        hour_index = minute_index // 60
        minute_in_hour = minute_index % 60
        value = 100 + 8 * math.sin(hour_index * 0.31) + 0.04 * hour_index + 0.001 * minute_in_hour
        close = Decimal(str(round(value, 8)))
        opened = start + timedelta(minutes=minute_index)
        candles.append(Candle(
            open_at_utc=opened,
            end_at_utc=opened + timedelta(minutes=1),
            open=previous_close,
            high=max(previous_close, close) + Decimal("0.05"),
            low=min(previous_close, close) - Decimal("0.05"),
            close=close,
            volume_base=Decimal(str(100 + (hour_index % 11) * 3 + minute_in_hour / 100)),
            instrument_id=instrument,
        ))
        previous_close = close
    return BacktestSnapshot(
        snapshot_id="synthetic-f6-smoke-v1",
        instrument_id=instrument,
        candles_1m=tuple(candles),
        content_sha256=hashlib.sha256(b"synthetic-f6-smoke-v1").hexdigest(),
        qa_status="PASS",
        data_origin="SYNTHETIC",
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="quantlab-f6-smoke-") as temp_dir:
        root = Path(temp_dir)
        snapshot = synthetic_snapshot()
        result = run_ml_experiment_on_snapshot(
            root,
            snapshot,
            timeframe="1h",
            horizon=6,
            train_start_at_utc="2024-01-01T00:00:00Z",
            train_end_at_utc_exclusive="2024-01-05T04:00:00Z",
            validation_start_at_utc="2024-01-05T04:00:00Z",
            validation_end_at_utc_exclusive="2024-01-09T08:00:00Z",
            initial_cash_usdt=Decimal("1000"),
            fee_bps_per_side=Decimal("10"),
            slippage_bps_per_side=Decimal("5"),
            assumed_data_delay_seconds=Decimal("1"),
            sma_fast=5,
            sma_slow=12,
        )
        manifest = result["manifest"]
        paths = result["paths"]
        required = (
            paths["experiment_json"],
            paths["threshold_trials_json"],
            paths["predictions_csv"],
            paths["report_html"],
            paths["backtest_run_dir"] / "fills.csv",
            paths["backtest_run_dir"] / "equity.csv",
        )
        if not all(path.is_file() for path in required):
            raise RuntimeError("smoke F6 não encontrou todos os artefatos do pipeline")
        if manifest["threshold_calibration"]["trial_count"] > manifest["threshold_calibration"]["trial_budget"]:
            raise RuntimeError("smoke F6 excedeu o orçamento de trials")
        if not manifest["fold"]["training_labels_observable_before_validation"]:
            raise RuntimeError("smoke F6 detectou label de treino disponível tarde demais")
        project = Path(__file__).resolve().parents[1]
        original_cwd = Path.cwd()
        try:
            os.chdir(root)
            app = AppTest.from_file(str(project / "src" / "quantlab" / "ui" / "app.py"), default_timeout=30)
            app.run()
            app.sidebar.radio[0].set_value("ML")
            app.run()
            if app.exception:
                raise AssertionError([item.message for item in app.exception])
            if not any("Resultados F6 salvos" in item.value for item in app.subheader):
                raise AssertionError("página ML não carregou os artefatos do experimento")
        finally:
            os.chdir(original_cwd)
        print(
            "F6_SMOKE_OK "
            f"experiment={manifest['experiment_id']} "
            f"train={manifest['sample_counts']['training_examples']} "
            f"validation={manifest['sample_counts']['validation_examples']} "
            f"trials={manifest['threshold_calibration']['trial_count']} "
            f"status={manifest['incremental_status']} "
            f"backtest={paths['backtest_run_id']} ui=ML"
        )


if __name__ == "__main__":
    main()
