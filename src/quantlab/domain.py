"""Tipos de domínio usados pelo simulador local do QuantLab."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} deve ser um datetime com fuso UTC")


def _require_finite(value: Decimal, field_name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field_name} deve ser um Decimal finito")


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class StrategyName(StrEnum):
    CASH = "CASH"
    BUY_HOLD = "BUY_HOLD"
    SMA_CROSS = "SMA_CROSS"
    MOMENTUM = "MOMENTUM"
    BREAKOUT = "BREAKOUT"
    MEAN_REVERSION = "MEAN_REVERSION"
    ML_NULL = "ML_NULL"
    ML_LOGISTIC_PRICE = "ML_LOGISTIC_PRICE"
    ML_LOGISTIC_PRICE_VOLUME = "ML_LOGISTIC_PRICE_VOLUME"


@dataclass(frozen=True, slots=True)
class Candle:
    open_at_utc: datetime
    end_at_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume_base: Decimal
    instrument_id: str = "SYNTH:SPOT:DEMOUSDT"

    def __post_init__(self) -> None:
        _require_utc(self.open_at_utc, "open_at_utc")
        _require_utc(self.end_at_utc, "end_at_utc")
        if self.end_at_utc <= self.open_at_utc:
            raise ValueError("end_at_utc deve ser posterior a open_at_utc")
        for name in ("open", "high", "low", "close", "volume_base"):
            _require_finite(getattr(self, name), name)
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("preços OHLC devem ser positivos")
        if self.volume_base < 0:
            raise ValueError("volume_base não pode ser negativo")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high não pode ser menor que outro preço OHLC")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low não pode ser maior que outro preço OHLC")
        if not self.instrument_id:
            raise ValueError("instrument_id não pode ser vazio")


@dataclass(frozen=True, slots=True)
class TargetExposure:
    fraction: Decimal

    def __post_init__(self) -> None:
        _require_finite(self.fraction, "target exposure")
        if self.fraction < 0 or self.fraction > 1:
            raise ValueError("target exposure deve estar entre 0 e 1")


@dataclass(frozen=True, slots=True)
class Signal:
    generated_at_utc: datetime
    available_at_utc: datetime
    eligible_at_utc: datetime
    target_exposure: TargetExposure
    reason: str

    def __post_init__(self) -> None:
        _require_utc(self.generated_at_utc, "generated_at_utc")
        _require_utc(self.available_at_utc, "available_at_utc")
        _require_utc(self.eligible_at_utc, "eligible_at_utc")
        if self.available_at_utc < self.generated_at_utc:
            raise ValueError("available_at_utc não pode anteceder o sinal")
        if self.eligible_at_utc <= self.available_at_utc:
            raise ValueError("fill elegível deve ocorrer estritamente após available_at")
        if not self.reason.strip():
            raise ValueError("reason do sinal não pode ser vazio")


@dataclass(frozen=True, slots=True)
class PortfolioState:
    cash_usdt: Decimal
    base_qty: Decimal
    fees_paid_usdt: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        for name in ("cash_usdt", "base_qty", "fees_paid_usdt"):
            _require_finite(getattr(self, name), name)
        if self.cash_usdt < 0:
            raise ValueError("cash_usdt não pode ficar negativo")
        if self.base_qty < 0:
            raise ValueError("base_qty não pode ficar negativa")
        if self.fees_paid_usdt < 0:
            raise ValueError("fees_paid_usdt não pode ficar negativa")


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    timestamp_utc: datetime
    side: Side
    quantity_base: Decimal
    price_usdt: Decimal
    quote_amount_usdt: Decimal
    fee_usdt: Decimal
    cash_after_usdt: Decimal
    base_qty_after: Decimal
    reference_price_usdt: Decimal
    slippage_cost_usdt: Decimal = Decimal("0")
    fee_currency: str = "USDT"

    def __post_init__(self) -> None:
        _require_utc(self.timestamp_utc, "timestamp_utc")
        for name in (
            "quantity_base",
            "price_usdt",
            "quote_amount_usdt",
            "fee_usdt",
            "cash_after_usdt",
            "base_qty_after",
            "reference_price_usdt",
            "slippage_cost_usdt",
        ):
            _require_finite(getattr(self, name), name)
        if self.quantity_base <= 0 or self.price_usdt <= 0:
            raise ValueError("fill deve ter quantidade e preço positivos")
        if self.quote_amount_usdt <= 0 or self.fee_usdt < 0:
            raise ValueError("notional deve ser positivo e fee não negativa")
        if self.cash_after_usdt < 0 or self.base_qty_after < 0:
            raise ValueError("fill não pode deixar saldo ou quantidade negativos")
        if self.reference_price_usdt <= 0 or self.slippage_cost_usdt < 0:
            raise ValueError("preço de referência inválido")
        if self.fee_currency != "USDT":
            raise ValueError("F0 cobra taxa somente em USDT")


@dataclass(frozen=True, slots=True)
class RunArtifacts:
    run_id: str
    directory: Path
    run_json: Path
    fills_csv: Path
    equity_csv: Path
    metrics_json: Path
    report_html: Path

