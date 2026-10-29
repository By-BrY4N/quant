"""Laço mínimo de backtest spot para CASH e BUY_HOLD."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from quantlab.domain import (
    Candle,
    PortfolioState,
    Signal,
    SimulatedFill,
    StrategyName,
    TargetExposure,
)
from quantlab.ledger import execute_market_target, mark_equity


DEFAULT_INITIAL_CASH_USDT = Decimal("1000.00")
DEFAULT_FEE_RATE = Decimal("0.001")
DEFAULT_SLIPPAGE_BPS_PER_SIDE = Decimal("5")
ASSUMED_DATA_DELAY = timedelta(seconds=1)
SIMULATED_BASE_QTY_STEP = Decimal("0.00000001")


@dataclass(frozen=True, slots=True)
class EquityPoint:
    strategy: StrategyName
    timestamp_utc: datetime
    cash_usdt: Decimal
    base_qty: Decimal
    mark_price_usdt: Decimal
    equity_usdt: Decimal
    fees_paid_usdt: Decimal


@dataclass(frozen=True, slots=True)
class StrategyMetrics:
    initial_equity_usdt: Decimal
    final_equity_usdt: Decimal
    net_pnl_usdt: Decimal
    net_return_pct: Decimal
    total_fees_usdt: Decimal
    total_slippage_cost_usdt: Decimal
    max_drawdown_pct: Decimal
    exposure_pct: Decimal
    fill_count: int
    final_cash_usdt: Decimal
    final_base_qty: Decimal


@dataclass(frozen=True, slots=True)
class BacktestResult:
    strategy: StrategyName
    signal: Signal
    fills: tuple[SimulatedFill, ...]
    equity: tuple[EquityPoint, ...]
    metrics: StrategyMetrics


def _validate_candles(candles: list[Candle]) -> None:
    if len(candles) < 20:
        raise ValueError("F0 exige pelo menos 20 candles para demonstrar a curva")
    for previous, current in zip(candles, candles[1:]):
        if current.open_at_utc != previous.end_at_utc:
            raise ValueError(
                "fixture contém gap ou sobreposição; F0 não preenche candles ausentes"
            )


def _max_drawdown_pct(points: list[EquityPoint], initial: Decimal) -> Decimal:
    peak = initial
    worst = Decimal("0")
    for point in points:
        peak = max(peak, point.equity_usdt)
        if peak > 0:
            drawdown = (peak - point.equity_usdt) / peak * Decimal("100")
            worst = max(worst, drawdown)
    return worst


def run_strategy(
    candles: list[Candle],
    strategy: StrategyName,
    *,
    initial_cash_usdt: Decimal = DEFAULT_INITIAL_CASH_USDT,
    fee_rate: Decimal = DEFAULT_FEE_RATE,
    slippage_bps_per_side: Decimal = DEFAULT_SLIPPAGE_BPS_PER_SIDE,
) -> BacktestResult:
    """Roda um sinal fixo de início e marca cada candle no fechamento.

    BUY_HOLD decide antes da primeira vela, fica elegível no primeiro open
    posterior a available_at e mantém a posição até o final. O laço nunca usa
    high/low/close da vela de execução para definir preço ou tamanho do fill.
    """
    _validate_candles(candles)
    if not initial_cash_usdt.is_finite() or initial_cash_usdt <= 0:
        raise ValueError("initial_cash_usdt deve ser positivo e finito")
    if not fee_rate.is_finite() or fee_rate < 0 or fee_rate >= 1:
        raise ValueError("fee_rate inválida")
    if not slippage_bps_per_side.is_finite() or slippage_bps_per_side < 0:
        raise ValueError("slippage_bps_per_side inválido")

    strategy = StrategyName(strategy)
    first = candles[0]
    generated_at = first.open_at_utc - timedelta(seconds=2)
    available_at = generated_at + ASSUMED_DATA_DELAY
    target = Decimal("1") if strategy is StrategyName.BUY_HOLD else Decimal("0")
    signal = Signal(
        generated_at_utc=generated_at,
        available_at_utc=available_at,
        eligible_at_utc=first.open_at_utc,
        target_exposure=TargetExposure(target),
        reason=(
            "compra única no início e manutenção até o fim"
            if strategy is StrategyName.BUY_HOLD
            else "permanecer em caixa"
        ),
    )

    state = PortfolioState(cash_usdt=initial_cash_usdt, base_qty=Decimal("0"))
    fills = []
    execution_bar = candles[0]
    if execution_bar.open_at_utc <= signal.available_at_utc:
        raise ValueError("open de execução não é posterior a available_at")
    if execution_bar.open_at_utc < signal.eligible_at_utc:
        raise ValueError("fill não pode anteceder o instante elegível")

    if signal.target_exposure.fraction == 1:
        state, fill, status = execute_market_target(
            state,
            timestamp_utc=execution_bar.open_at_utc,
            target_fraction=signal.target_exposure.fraction,
            reference_open_usdt=execution_bar.open,
            fee_rate=fee_rate,
            slippage_rate=slippage_bps_per_side / Decimal("10000"),
            quantity_step=SIMULATED_BASE_QTY_STEP,
        )
        if status != "FILLED" or fill is None:
            raise ValueError("saldo insuficiente para a quantidade simulada mínima")
        fills.append(fill)

    equity_points: list[EquityPoint] = []
    for candle in candles:
        equity = mark_equity(state, candle.close)
        equity_points.append(
            EquityPoint(
                strategy=strategy,
                timestamp_utc=candle.end_at_utc,
                cash_usdt=state.cash_usdt,
                base_qty=state.base_qty,
                mark_price_usdt=candle.close,
                equity_usdt=equity,
                fees_paid_usdt=state.fees_paid_usdt,
            )
        )

    final_equity = equity_points[-1].equity_usdt
    pnl = final_equity - initial_cash_usdt
    return_pct = pnl / initial_cash_usdt * Decimal("100")
    exposed_bars = sum(1 for point in equity_points if point.base_qty > 0)
    exposure_pct = Decimal(exposed_bars) / Decimal(len(equity_points)) * Decimal("100")
    metrics = StrategyMetrics(
        initial_equity_usdt=initial_cash_usdt,
        final_equity_usdt=final_equity,
        net_pnl_usdt=pnl,
        net_return_pct=return_pct,
        total_fees_usdt=state.fees_paid_usdt,
        total_slippage_cost_usdt=sum(
            (fill.slippage_cost_usdt for fill in fills), start=Decimal("0")
        ),
        max_drawdown_pct=_max_drawdown_pct(equity_points, initial_cash_usdt),
        exposure_pct=exposure_pct,
        fill_count=len(fills),
        final_cash_usdt=state.cash_usdt,
        final_base_qty=state.base_qty,
    )
    return BacktestResult(
        strategy=strategy,
        signal=signal,
        fills=tuple(fills),
        equity=tuple(equity_points),
        metrics=metrics,
    )

