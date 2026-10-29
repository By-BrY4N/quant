"""Fixture pública offline. Todos os candles desta função são SINTÉTICOS."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from quantlab.domain import Candle


SYNTHETIC_DATASET_ID = "synthetic-demo-v1"
SYNTHETIC_NOTICE = (
    "Dados artificiais gerados por fórmula determinística; não são dados de exchange "
    "e não constituem evidência de desempenho."
)


def synthetic_demo_candles(count: int = 120) -> list[Candle]:
    """Cria 120 velas sintéticas de 1 minuto, suficientes para a curva da demo."""
    if count < 20:
        raise ValueError("a fixture visual precisa de ao menos 20 candles")

    start = datetime(2026, 1, 1, tzinfo=UTC)
    previous_close = Decimal("100.00")
    candles: list[Candle] = []
    for index in range(count):
        raw_close = 100 + index * 0.035 + 3.4 * math.sin(index / 7) + 1.1 * math.sin(index / 2.4)
        close = Decimal(str(round(raw_close, 2)))
        open_price = previous_close
        wick = Decimal("0.35") + Decimal(index % 4) / Decimal("100")
        high = max(open_price, close) + wick
        low = min(open_price, close) - wick
        timestamp = start + timedelta(minutes=index)
        candles.append(
            Candle(
                open_at_utc=timestamp,
                end_at_utc=timestamp + timedelta(minutes=1),
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume_base=Decimal("5.0") + Decimal(index % 11) / Decimal("10"),
                instrument_id="SYNTH:SPOT:DEMOUSDT",
            )
        )
        previous_close = close
    return candles
