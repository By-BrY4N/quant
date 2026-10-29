"""Ledger determinístico de spot long-only, sem broker nem ordens reais."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_DOWN

from quantlab.domain import PortfolioState, Side, SimulatedFill


def apply_fill(
    state: PortfolioState,
    *,
    timestamp_utc: datetime,
    side: Side,
    quantity_base: Decimal,
    price_usdt: Decimal,
    fee_rate: Decimal,
    reference_price_usdt: Decimal | None = None,
) -> tuple[PortfolioState, SimulatedFill]:
    """Aplica um fill integral e cobra a taxa em USDT uma única vez."""
    for name, value in (
        ("quantity_base", quantity_base),
        ("price_usdt", price_usdt),
        ("fee_rate", fee_rate),
    ):
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError(f"{name} deve ser um Decimal finito")
    if quantity_base <= 0 or price_usdt <= 0:
        raise ValueError("quantidade e preço do fill devem ser positivos")
    if fee_rate < 0 or fee_rate >= 1:
        raise ValueError("fee_rate deve estar entre 0 (inclusivo) e 1 (exclusivo)")

    notional = quantity_base * price_usdt
    fee = notional * fee_rate
    if side is Side.BUY:
        total_debit = notional + fee
        if total_debit > state.cash_usdt:
            raise ValueError("caixa insuficiente para compra incluindo fee")
        next_state = PortfolioState(
            cash_usdt=state.cash_usdt - total_debit,
            base_qty=state.base_qty + quantity_base,
            fees_paid_usdt=state.fees_paid_usdt + fee,
        )
    elif side is Side.SELL:
        if quantity_base > state.base_qty:
            raise ValueError("quantidade insuficiente; F0 não permite short")
        next_state = PortfolioState(
            cash_usdt=state.cash_usdt + notional - fee,
            base_qty=state.base_qty - quantity_base,
            fees_paid_usdt=state.fees_paid_usdt + fee,
        )
    else:
        raise ValueError(f"lado de fill não suportado: {side}")

    reference = price_usdt if reference_price_usdt is None else reference_price_usdt
    fill = SimulatedFill(
        timestamp_utc=timestamp_utc,
        side=side,
        quantity_base=quantity_base,
        price_usdt=price_usdt,
        quote_amount_usdt=notional,
        fee_usdt=fee,
        cash_after_usdt=next_state.cash_usdt,
        base_qty_after=next_state.base_qty,
        reference_price_usdt=reference,
        slippage_cost_usdt=quantity_base * abs(price_usdt - reference),
    )
    return next_state, fill


def execute_market_target(
    state: PortfolioState,
    *,
    timestamp_utc: datetime,
    target_fraction: Decimal,
    reference_open_usdt: Decimal,
    fee_rate: Decimal,
    slippage_rate: Decimal,
    quantity_step: Decimal,
) -> tuple[PortfolioState, SimulatedFill | None, str]:
    """Aplica target 0/1 pela semântica única de market proxy do simulador.

    A função recebe apenas o open de referência; high, low, close e volume da
    barra de execução não participam da decisão, sizing ou preço do fill.
    """
    values = {
        "target_fraction": target_fraction,
        "reference_open_usdt": reference_open_usdt,
        "fee_rate": fee_rate,
        "slippage_rate": slippage_rate,
        "quantity_step": quantity_step,
    }
    for name, value in values.items():
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError(f"{name} deve ser um Decimal finito")
    if target_fraction not in {Decimal("0"), Decimal("1")}:
        raise ValueError("F3 aceita somente target 0 ou 1")
    if reference_open_usdt <= 0:
        raise ValueError("reference_open_usdt deve ser positivo")
    if fee_rate < 0 or fee_rate >= 1:
        raise ValueError("fee_rate deve estar entre 0 (inclusivo) e 1 (exclusivo)")
    if slippage_rate < 0 or slippage_rate >= 1:
        raise ValueError("slippage_rate deve estar entre 0 (inclusivo) e 1 (exclusivo)")
    if quantity_step <= 0:
        raise ValueError("quantity_step deve ser positivo")

    target_is_long = target_fraction == Decimal("1")
    if target_is_long == (state.base_qty > 0):
        return state, None, "TARGET_UNCHANGED"

    side = Side.BUY if target_is_long else Side.SELL
    execution_price = reference_open_usdt * (
        Decimal("1") + slippage_rate
        if side is Side.BUY
        else Decimal("1") - slippage_rate
    )
    if side is Side.BUY:
        affordable = state.cash_usdt / (execution_price * (Decimal("1") + fee_rate))
        quantity = (
            (affordable / quantity_step).to_integral_value(rounding=ROUND_DOWN)
            * quantity_step
        )
        if quantity <= 0:
            return state, None, "INSUFFICIENT_CASH"
    else:
        quantity = state.base_qty

    next_state, fill = apply_fill(
        state,
        timestamp_utc=timestamp_utc,
        side=side,
        quantity_base=quantity,
        price_usdt=execution_price,
        fee_rate=fee_rate,
        reference_price_usdt=reference_open_usdt,
    )
    return next_state, fill, "FILLED"


def mark_equity(state: PortfolioState, mark_price_usdt: Decimal) -> Decimal:
    if not mark_price_usdt.is_finite() or mark_price_usdt <= 0:
        raise ValueError("mark_price_usdt deve ser positivo e finito")
    return state.cash_usdt + state.base_qty * mark_price_usdt
