from __future__ import annotations

import hashlib
import math
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from quantlab.backtest import BacktestSnapshot
from quantlab.domain import Candle
from quantlab.ml import (
    build_labeled_samples,
    fit_predict_probabilities,
)


def _synthetic_snapshot(hours: int, *, mutate_from_hour: int | None = None) -> BacktestSnapshot:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    instrument = "SYNTH:SPOT:MLTESTUSDT"
    candles: list[Candle] = []
    previous_close = Decimal("100")
    for minute_index in range(hours * 60):
        hour_index = minute_index // 60
        minute_in_hour = minute_index % 60
        price = 100 + 8 * math.sin(hour_index * 0.31) + 0.04 * hour_index + 0.001 * minute_in_hour
        if mutate_from_hour is not None and hour_index >= mutate_from_hour:
            price *= 1.2
        close = Decimal(str(round(price, 8)))
        open_price = previous_close
        high = max(open_price, close) + Decimal("0.05")
        low = min(open_price, close) - Decimal("0.05")
        volume = Decimal(str(100 + (hour_index % 11) * 3 + minute_in_hour / 100))
        opened = start + timedelta(minutes=minute_index)
        candles.append(Candle(
            open_at_utc=opened,
            end_at_utc=opened + timedelta(minutes=1),
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume_base=volume,
            instrument_id=instrument,
        ))
        previous_close = close
    content = f"synthetic-f6-{hours}-{mutate_from_hour}".encode("ascii")
    return BacktestSnapshot(
        snapshot_id=f"synthetic-f6-{hours}-{mutate_from_hour}",
        instrument_id=instrument,
        candles_1m=tuple(candles),
        content_sha256=hashlib.sha256(content).hexdigest(),
        qa_status="PASS",
        data_origin="SYNTHETIC",
    )


class F6TemporalPredictionTests(unittest.TestCase):
    def test_future_values_do_not_change_earlier_validation_probabilities(self) -> None:
        original = _synthetic_snapshot(190)
        altered = _synthetic_snapshot(190, mutate_from_hour=145)
        options = {
            "timeframe": "1h",
            "horizon": 6,
            "assumed_data_delay_seconds": Decimal("1"),
            "fee_bps_per_side": Decimal("10"),
            "slippage_bps_per_side": Decimal("5"),
        }
        original_samples = build_labeled_samples(original, **options)
        altered_samples = build_labeled_samples(altered, **options)
        validation_start = original_samples[75].available_at_utc
        validation_end = original_samples[-4].available_at_utc
        train = [
            sample for sample in original_samples
            if sample.available_at_utc < validation_start
            and sample.label_observable_at_utc < validation_start
        ]
        validation = [
            sample for sample in original_samples
            if validation_start <= sample.available_at_utc < validation_end
        ]
        altered_train = [
            sample for sample in altered_samples
            if sample.available_at_utc < validation_start
            and sample.label_observable_at_utc < validation_start
        ]
        altered_validation = [
            sample for sample in altered_samples
            if validation_start <= sample.available_at_utc < validation_end
        ]
        self.assertGreaterEqual(len(train), 40)
        self.assertEqual([sample.sample_id for sample in train], [sample.sample_id for sample in altered_train])
        self.assertEqual([sample.sample_id for sample in validation], [sample.sample_id for sample in altered_validation])

        original_probabilities = fit_predict_probabilities(train, validation)
        altered_probabilities = fit_predict_probabilities(altered_train, altered_validation)
        cutoff = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=140)
        prior_indices = [
            index for index, sample in enumerate(validation)
            if sample.available_at_utc < cutoff
        ]
        self.assertGreater(len(prior_indices), 10)
        for model_name in original_probabilities:
            for index in prior_indices:
                self.assertAlmostEqual(
                    original_probabilities[model_name][index],
                    altered_probabilities[model_name][index],
                    places=12,
                    msg=f"predição anterior mudou: {model_name}, {validation[index].sample_id}",
                )


if __name__ == "__main__":
    unittest.main()
