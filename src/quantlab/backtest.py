"""Backtest F3 spot, causal e com ledger compartilhado pelo fluxo canônico."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Iterable, Mapping

from quantlab.domain import Candle, PortfolioState, Side, SimulatedFill, StrategyName, TargetExposure
from quantlab.engine import SIMULATED_BASE_QTY_STEP
from quantlab.ledger import execute_market_target, mark_equity

MINUTE = timedelta(minutes=1)
TIMEFRAME_MINUTES = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440}
DAILY_SHARPE_ANNUALIZATION_DAYS = 365
DAILY_SHARPE_RISK_FREE_RETURN = Decimal("0")
DAILY_SHARPE_RETURN_FREQUENCY = "UTC_DAILY_SIMPLE"
EXTERNAL_TARGET_STRATEGIES = frozenset({
    StrategyName.ML_NULL,
    StrategyName.ML_LOGISTIC_PRICE,
    StrategyName.ML_LOGISTIC_PRICE_VOLUME,
})


@dataclass(frozen=True, slots=True)
class BacktestSnapshot:
    """Entrada imutável do backtest, construída somente de snapshot QA PASS."""

    snapshot_id: str
    instrument_id: str
    candles_1m: tuple[Candle, ...]
    content_sha256: str
    qa_status: str
    data_origin: str = "REAL"
    source_metadata: Mapping[str, str] | None = None
    source_manifest: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not self.snapshot_id.strip() or not self.instrument_id.strip():
            raise ValueError("snapshot_id e instrument_id são obrigatórios")
        if self.qa_status != "PASS":
            raise ValueError("backtest exige snapshot com QA PASS")
        if len(self.content_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.content_sha256.lower()
        ):
            raise ValueError("content_sha256 do snapshot deve ser SHA-256 hexadecimal")
        if self.data_origin not in {"REAL", "SYNTHETIC"}:
            raise ValueError("data_origin deve ser REAL ou SYNTHETIC")
        if not self.candles_1m:
            raise ValueError("snapshot não contém candles 1m")

        previous: Candle | None = None
        for candle in self.candles_1m:
            if candle.instrument_id != self.instrument_id:
                raise ValueError("instrument_id do candle diverge do snapshot")
            if candle.open_at_utc.second or candle.open_at_utc.microsecond:
                raise ValueError("open_at_utc deve estar alinhado ao minuto UTC")
            if candle.end_at_utc - candle.open_at_utc != MINUTE:
                raise ValueError("snapshot F3 aceita somente candles de 1 minuto")
            if previous is not None:
                if candle.open_at_utc <= previous.open_at_utc:
                    raise ValueError("candles do snapshot devem estar ordenados sem duplicatas")
                if candle.open_at_utc < previous.end_at_utc:
                    raise ValueError("candles do snapshot não podem se sobrepor")
            previous = candle


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    strategy: StrategyName
    decision_timeframe: str = "1h"
    initial_cash_usdt: Decimal = Decimal("1000.00")
    fee_bps_per_side: Decimal = Decimal("10")
    slippage_bps_per_side: Decimal = Decimal("5")
    assumed_data_delay_seconds: Decimal = Decimal("1")
    sma_fast: int = 20
    sma_slow: int = 50
    breakout_lookback: int = 20
    momentum_lookback: int = 24
    mean_reversion_lookback: int = 30
    mean_reversion_entry_z: Decimal = Decimal("-2")
    mean_reversion_exit_z: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy", StrategyName(self.strategy))
        delay_seconds = self.assumed_data_delay_seconds
        if not isinstance(delay_seconds, Decimal):
            try:
                delay_seconds = Decimal(str(delay_seconds))
            except Exception as error:
                raise ValueError("assumed_data_delay_seconds deve ser Decimal finito") from error
            object.__setattr__(self, "assumed_data_delay_seconds", delay_seconds)
        if self.decision_timeframe not in TIMEFRAME_MINUTES:
            raise ValueError("decision_timeframe deve ser 15m, 1h, 4h ou 1d")
        for name, value in (
            ("initial_cash_usdt", self.initial_cash_usdt),
            ("fee_bps_per_side", self.fee_bps_per_side),
            ("slippage_bps_per_side", self.slippage_bps_per_side),
        ):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{name} deve ser Decimal finito")
        if self.initial_cash_usdt <= 0:
            raise ValueError("capital inicial deve ser positivo")
        if self.fee_bps_per_side < 0 or self.fee_bps_per_side >= 10000:
            raise ValueError("fee_bps_per_side deve estar entre 0 e 10000")
        if self.slippage_bps_per_side < 0 or self.slippage_bps_per_side >= 10000:
            raise ValueError("slippage_bps_per_side deve estar entre 0 e 10000")
        if not delay_seconds.is_finite() or delay_seconds <= 0:
            raise ValueError("assumed_data_delay_seconds deve ser positivo e finito")
        delay_microseconds = delay_seconds * Decimal("1000000")
        if delay_microseconds != delay_microseconds.to_integral_value():
            raise ValueError("assumed_data_delay_seconds deve ter precisão de microssegundos")
        if self.sma_fast < 1 or self.sma_slow <= self.sma_fast:
            raise ValueError("SMA exige 1 <= fast < slow")
        if self.breakout_lookback < 1:
            raise ValueError("breakout_lookback deve ser positivo")
        if self.momentum_lookback < 1:
            raise ValueError("momentum_lookback deve ser positivo")
        if self.mean_reversion_lookback < 2:
            raise ValueError("mean_reversion_lookback deve ser pelo menos 2")
        for name in ("mean_reversion_entry_z", "mean_reversion_exit_z"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                try:
                    value = Decimal(str(value))
                except Exception as error:
                    raise ValueError(f"{name} deve ser um Decimal finito") from error
                object.__setattr__(self, name, value)
            if not value.is_finite():
                raise ValueError(f"{name} deve ser um Decimal finito")
        if self.mean_reversion_entry_z >= self.mean_reversion_exit_z:
            raise ValueError("mean_reversion_entry_z deve ser menor que mean_reversion_exit_z")


@dataclass(frozen=True, slots=True)
class DecisionCandle:
    open_at_utc: datetime
    end_at_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume_base: Decimal
    constituents: int


@dataclass(slots=True)
class SignalEvent:
    strategy: StrategyName
    generated_at_utc: datetime
    available_at_utc: datetime
    eligible_at_utc: datetime
    target_exposure: Decimal
    reason: str
    execution_status: str = "PENDING"
    execution_at_utc: datetime | None = None
    veto_reason: str | None = None


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
    gross_pnl_usdt: Decimal
    total_fees_usdt: Decimal
    total_slippage_cost_usdt: Decimal
    net_pnl_usdt: Decimal
    net_return_pct: Decimal
    max_drawdown_pct: Decimal
    exposure_pct: Decimal
    turnover_quote_usdt: Decimal
    buy_count: int
    sell_count: int
    veto_count: int
    open_position: bool
    final_cash_usdt: Decimal
    final_base_qty: Decimal
    daily_sharpe: Decimal | None = None
    daily_sharpe_observation_count: int = 0
    daily_sharpe_annualization_days: int = DAILY_SHARPE_ANNUALIZATION_DAYS
    daily_sharpe_risk_free_daily_return: Decimal = DAILY_SHARPE_RISK_FREE_RETURN
    daily_sharpe_return_frequency: str = DAILY_SHARPE_RETURN_FREQUENCY
    win_rate_pct: Decimal | None = None
    profit_factor: Decimal | None = None
    closed_trade_count: int = 0


@dataclass(frozen=True, slots=True)
class StrategyResult:
    strategy: StrategyName
    signals: tuple[SignalEvent, ...]
    fills: tuple[SimulatedFill, ...]
    equity: tuple[EquityPoint, ...]
    metrics: StrategyMetrics


@dataclass(frozen=True, slots=True)
class BacktestResult:
    config: BacktestConfig
    snapshot: BacktestSnapshot
    results: tuple[StrategyResult, ...]
    audit: Mapping[str, object]


def closed_trade_pnl_by_exit_timestamp(
    fills: Iterable[SimulatedFill],
) -> dict[datetime, Decimal]:
    """Return realized quote-currency P&L for closed long round trips.

    Entry basis includes the buy notional and fee. Exit proceeds subtract the
    sell fee. Slippage is already reflected in each fill's execution notional,
    so it is not charged a second time. Open positions have no realized P&L.
    """
    open_quantity = Decimal("0")
    open_cost = Decimal("0")
    previous_timestamp: datetime | None = None
    realized: dict[datetime, Decimal] = {}

    for fill in fills:
        if previous_timestamp is not None and fill.timestamp_utc < previous_timestamp:
            raise ValueError("fills devem estar em ordem cronológica para calcular P&L realizado")
        previous_timestamp = fill.timestamp_utc

        if fill.side is Side.BUY:
            open_quantity += fill.quantity_base
            open_cost += fill.quote_amount_usdt + fill.fee_usdt
            continue

        if fill.quantity_base > open_quantity:
            raise ValueError("fill SELL excede a quantidade comprada ainda aberta")
        if open_quantity <= 0:
            raise ValueError("fill SELL sem posição comprada aberta")

        allocated_cost = open_cost * fill.quantity_base / open_quantity
        realized[fill.timestamp_utc] = fill.quote_amount_usdt - fill.fee_usdt - allocated_cost
        open_quantity -= fill.quantity_base
        open_cost -= allocated_cost

    return realized


def _strictly_next_minute_open(available_at: datetime) -> datetime:
    minute_floor = available_at.replace(second=0, microsecond=0)
    return minute_floor + MINUTE


def _aggregate_decision_candles(
    candles: tuple[Candle, ...], timeframe: str
) -> tuple[list[DecisionCandle], int]:
    minutes = TIMEFRAME_MINUTES[timeframe]
    width = MINUTE * minutes
    grouped: dict[datetime, list[Candle]] = {}
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    for candle in candles:
        elapsed = candle.open_at_utc - epoch
        bucket = epoch + width * (elapsed // width)
        grouped.setdefault(bucket, []).append(candle)

    completed: list[DecisionCandle] = []
    if grouped:
        first_bucket = min(grouped)
        last_bucket = max(grouped)
        expected_bucket_count = int((last_bucket - first_bucket) // width) + 1
        # Count wholly absent buckets as incomplete too; they have no group
        # to visit below but still represent blocked decision windows.
        discarded = expected_bucket_count - len(grouped)
    else:
        discarded = 0
    for bucket, members in sorted(grouped.items()):
        expected_starts = [bucket + MINUTE * index for index in range(minutes)]
        complete = (
            members[0].open_at_utc == bucket
            and len(members) == minutes
            and all(
                member.open_at_utc == expected
                and member.end_at_utc == expected + MINUTE
                for member, expected in zip(members, expected_starts, strict=True)
            )
            and members[-1].end_at_utc == bucket + width
        )
        if not complete:
            discarded += 1
            continue
        completed.append(
            DecisionCandle(
                open_at_utc=bucket,
                end_at_utc=bucket + width,
                open=members[0].open,
                high=max(member.high for member in members),
                low=min(member.low for member in members),
                close=members[-1].close,
                volume_base=sum(
                    (member.volume_base for member in members), start=Decimal("0")
                ),
                constituents=len(members),
            )
        )
    return completed, discarded


def _strategy_signals(
    strategy: StrategyName,
    decision_candles: list[DecisionCandle],
    first_minute: Candle,
    config: BacktestConfig,
) -> tuple[list[SignalEvent], int]:
    delay = timedelta(microseconds=int(config.assumed_data_delay_seconds * Decimal("1000000")))
    signals: list[SignalEvent] = []
    warmup_discarded = 0

    if strategy in {StrategyName.CASH, StrategyName.BUY_HOLD}:
        target = Decimal("1") if strategy is StrategyName.BUY_HOLD else Decimal("0")
        available = first_minute.open_at_utc + delay
        signals.append(
            SignalEvent(
                strategy=strategy,
                generated_at_utc=first_minute.open_at_utc,
                available_at_utc=available,
                eligible_at_utc=_strictly_next_minute_open(available),
                target_exposure=target,
                reason=(
                    "baseline compra uma vez na primeira execução elegível e mantém"
                    if strategy is StrategyName.BUY_HOLD
                    else "baseline mantém todo o capital em caixa, sem juros"
                ),
            )
        )
        return signals, 0

    segment_start = 0
    in_mean_reversion_position = False
    for index, candle in enumerate(decision_candles):
        if index and candle.open_at_utc != decision_candles[index - 1].end_at_utc:
            # Um gap quebra a janela causal. Recomeçar warm-up após a região
            # contaminada impede que barras não adjacentes virem vizinhas.
            segment_start = index
        bars_in_segment = index - segment_start + 1
        target: Decimal | None = None
        if strategy is StrategyName.SMA_CROSS:
            if bars_in_segment < config.sma_slow:
                warmup_discarded += 1
                continue
            fast_values = [
                item.close
                for item in decision_candles[index + 1 - config.sma_fast : index + 1]
            ]
            slow_values = [
                item.close
                for item in decision_candles[index + 1 - config.sma_slow : index + 1]
            ]
            fast_mean = sum(fast_values, start=Decimal("0")) / Decimal(len(fast_values))
            slow_mean = sum(slow_values, start=Decimal("0")) / Decimal(len(slow_values))
            target = Decimal("1") if fast_mean > slow_mean else Decimal("0")
            reason = (
                f"SMA({config.sma_fast}) acima de SMA({config.sma_slow})"
                if target == 1
                else f"SMA({config.sma_fast}) não está acima de SMA({config.sma_slow})"
            )
        elif strategy is StrategyName.MOMENTUM:
            lookback = config.momentum_lookback
            if bars_in_segment <= lookback:
                warmup_discarded += 1
                continue
            prior_close = decision_candles[index - lookback].close
            target = Decimal("1") if candle.close > prior_close else Decimal("0")
            reason = (
                f"momentum positivo: close atual acima do close de {lookback} barras atrás"
                if target == 1
                else f"momentum não positivo: close atual não está acima do close de {lookback} barras atrás"
            )
        elif strategy is StrategyName.BREAKOUT:
            lookback = config.breakout_lookback
            if bars_in_segment <= lookback:
                warmup_discarded += 1
                continue
            prior_high = max(
                item.high for item in decision_candles[index - lookback : index]
            )
            target = Decimal("1") if candle.close > prior_high else Decimal("0")
            reason = (
                f"close rompe máxima das {lookback} barras completas anteriores"
                if target == 1
                else f"sem rompimento; breakout sai para caixa (regra F3 v1, lookback={lookback})"
            )
        elif strategy is StrategyName.MEAN_REVERSION:
            lookback = config.mean_reversion_lookback
            if bars_in_segment < lookback:
                warmup_discarded += 1
                continue
            window = [
                item.close
                for item in decision_candles[index + 1 - lookback : index + 1]
            ]
            mean = sum(window, start=Decimal("0")) / Decimal(lookback)
            variance = sum(
                ((value - mean) * (value - mean) for value in window),
                start=Decimal("0"),
            ) / Decimal(lookback)
            standard_deviation = variance.sqrt()
            if standard_deviation == 0:
                reason = (
                    "z-score indefinido (desvio padrão populacional trailing zero; "
                    "janela inclui a barra atual); manter alvo anterior"
                )
            else:
                z_score = (candle.close - mean) / standard_deviation
                if (
                    not in_mean_reversion_position
                    and z_score <= config.mean_reversion_entry_z
                ):
                    in_mean_reversion_position = True
                elif in_mean_reversion_position and z_score >= config.mean_reversion_exit_z:
                    in_mean_reversion_position = False
                reason = (
                    f"z-score trailing populacional={z_score} (janela inclui a barra atual); "
                    f"entrada <= {config.mean_reversion_entry_z}; "
                    f"saída >= {config.mean_reversion_exit_z}; "
                    f"{'em posição' if in_mean_reversion_position else 'em caixa'}"
                )
            target = Decimal("1") if in_mean_reversion_position else Decimal("0")
        else:
            raise ValueError(f"estratégia não suportada no backtest: {strategy}")

        available = candle.end_at_utc + delay
        signals.append(
            SignalEvent(
                strategy=strategy,
                generated_at_utc=candle.end_at_utc,
                available_at_utc=available,
                eligible_at_utc=_strictly_next_minute_open(available),
                target_exposure=target,
                reason=reason,
            )
        )
    return signals, warmup_discarded


def _external_target_signals(
    strategy: StrategyName,
    decision_candles: list[DecisionCandle],
    config: BacktestConfig,
    targets_by_decision_end: Mapping[datetime, Decimal | TargetExposure],
) -> list[SignalEvent]:
    """Wrap externally generated, timestamped targets in the canonical F3 clock."""
    if strategy not in EXTERNAL_TARGET_STRATEGIES:
        raise ValueError(f"alvos externos não são permitidos para {strategy.value}")
    delay = timedelta(microseconds=int(config.assumed_data_delay_seconds * Decimal("1000000")))
    decisions_by_end = {item.end_at_utc: item for item in decision_candles}
    signals: list[SignalEvent] = []
    for decision_end, target in sorted(targets_by_decision_end.items()):
        if decision_end.tzinfo is None or decision_end.utcoffset() != timedelta(0):
            raise ValueError("timestamp de alvo ML deve conter timezone UTC")
        if decision_end not in decisions_by_end:
            raise ValueError("alvo ML não corresponde a uma barra de decisão completa")
        exposure = target if isinstance(target, TargetExposure) else TargetExposure(Decimal(str(target)))
        target_value = exposure.fraction
        if target_value not in {Decimal("0"), Decimal("1")}:
            raise ValueError("alvo ML deve ser zero ou um")
        available = decision_end + delay
        signals.append(
            SignalEvent(
                strategy=strategy,
                generated_at_utc=decision_end,
                available_at_utc=available,
                eligible_at_utc=_strictly_next_minute_open(available),
                target_exposure=target_value,
                reason=f"target supervisionado F6 pré-computado ({strategy.value})",
            )
        )
    return signals


def _max_drawdown(points: list[EquityPoint], initial: Decimal) -> Decimal:
    peak = initial
    worst = Decimal("0")
    for point in points:
        peak = max(peak, point.equity_usdt)
        if peak:
            worst = max(worst, (peak - point.equity_usdt) / peak * Decimal("100"))
    return worst


def _daily_simple_returns(
    candles: tuple[Candle, ...],
    equity: list[EquityPoint],
    initial_equity: Decimal,
) -> list[Decimal]:
    """Build only complete, consecutive UTC-day close-to-close observations.

    The initial cash is a valid first anchor only when the snapshot starts at
    00:00 UTC and provides all 1,440 minute marks through the following UTC
    midnight. Any missing minute breaks the daily series.
    """
    daily_points = [
        (index, point)
        for index, point in enumerate(equity)
        if point.timestamp_utc.time() == time.min
    ]
    if not daily_points:
        return []

    returns: list[Decimal] = []
    previous_daily: tuple[int, EquityPoint] | None = None
    first_open = candles[0].open_at_utc
    first_daily_index, first_daily_point = daily_points[0]
    if (
        first_open.time() == time.min
        and first_daily_index == 1439
        and first_daily_point.timestamp_utc == first_open + timedelta(days=1)
    ):
        returns.append(first_daily_point.equity_usdt / initial_equity - Decimal("1"))
        previous_daily = (first_daily_index, first_daily_point)

    for current in daily_points:
        if previous_daily is None:
            previous_daily = current
            continue
        previous_index, previous_point = previous_daily
        current_index, current_point = current
        if (
            current_index - previous_index == 1440
            and current_point.timestamp_utc - previous_point.timestamp_utc == timedelta(days=1)
        ):
            returns.append(
                current_point.equity_usdt / previous_point.equity_usdt - Decimal("1")
            )
        previous_daily = current
    return returns


def _daily_sharpe(returns: list[Decimal]) -> Decimal | None:
    """Annualized Sharpe of simple daily returns, with a zero daily risk-free rate."""
    if len(returns) < 2:
        return None
    mean_excess_return = (
        sum(returns, start=Decimal("0")) / Decimal(len(returns))
        - DAILY_SHARPE_RISK_FREE_RETURN
    )
    sample_variance = sum(
        ((value - (mean_excess_return + DAILY_SHARPE_RISK_FREE_RETURN)) ** 2 for value in returns),
        start=Decimal("0"),
    ) / Decimal(len(returns) - 1)
    if sample_variance <= 0:
        return None
    return (
        mean_excess_return
        / sample_variance.sqrt()
        * Decimal(DAILY_SHARPE_ANNUALIZATION_DAYS).sqrt()
    )


def _simulate_strategy(
    candles: tuple[Candle, ...],
    strategy: StrategyName,
    signals: list[SignalEvent],
    config: BacktestConfig,
) -> StrategyResult:
    bars_by_open = {candle.open_at_utc: candle for candle in candles}
    scheduled: dict[datetime, list[SignalEvent]] = {}
    for signal in signals:
        if signal.execution_status.startswith("VETOED"):
            continue
        eligible = signal.eligible_at_utc
        execution_bar = bars_by_open.get(eligible)
        previous_bar = bars_by_open.get(eligible - MINUTE)
        if execution_bar is None:
            signal.execution_status = "VETOED_MISSING_EXECUTION_BAR"
            signal.veto_reason = "não existe candle 1m no primeiro open estritamente elegível"
            continue
        if previous_bar is None or previous_bar.end_at_utc != eligible:
            signal.execution_status = "VETOED_GAP_BEFORE_EXECUTION"
            signal.veto_reason = "há lacuna no candle 1m que antecede o open elegível"
            continue
        if eligible <= signal.available_at_utc or eligible <= signal.generated_at_utc:
            raise ValueError("invariante temporal violada: fill não posterior ao sinal/dado")
        scheduled.setdefault(eligible, []).append(signal)

    state = PortfolioState(cash_usdt=config.initial_cash_usdt, base_qty=Decimal("0"))
    fills: list[SimulatedFill] = []
    equity: list[EquityPoint] = []
    fee_rate = config.fee_bps_per_side / Decimal("10000")
    slip_rate = config.slippage_bps_per_side / Decimal("10000")

    for candle in candles:
        for signal in scheduled.get(candle.open_at_utc, []):
            state, fill, status = execute_market_target(
                state,
                timestamp_utc=candle.open_at_utc,
                target_fraction=signal.target_exposure,
                reference_open_usdt=candle.open,
                fee_rate=fee_rate,
                slippage_rate=slip_rate,
                quantity_step=SIMULATED_BASE_QTY_STEP,
            )
            if status == "TARGET_UNCHANGED":
                signal.execution_status = "NO_ORDER_TARGET_UNCHANGED"
                continue
            if status == "INSUFFICIENT_CASH":
                signal.execution_status = "VETOED_INSUFFICIENT_CASH"
                signal.veto_reason = "caixa insuficiente para a menor quantidade simulada"
                continue
            if status != "FILLED" or fill is None:
                raise ValueError(f"resultado de execução desconhecido: {status}")
            fills.append(fill)
            signal.execution_status = "FILLED"
            signal.execution_at_utc = fill.timestamp_utc

        mark = candle.close
        equity.append(
            EquityPoint(
                strategy=strategy,
                timestamp_utc=candle.end_at_utc,
                cash_usdt=state.cash_usdt,
                base_qty=state.base_qty,
                mark_price_usdt=mark,
                equity_usdt=mark_equity(state, mark),
                fees_paid_usdt=state.fees_paid_usdt,
            )
        )

    final_equity = equity[-1].equity_usdt
    net_pnl = final_equity - config.initial_cash_usdt
    fees = state.fees_paid_usdt
    slippage = sum((fill.slippage_cost_usdt for fill in fills), start=Decimal("0"))
    daily_returns = _daily_simple_returns(candles, equity, config.initial_cash_usdt)
    closed_trade_pnls = tuple(closed_trade_pnl_by_exit_timestamp(fills).values())
    closed_trade_count = len(closed_trade_pnls)
    win_rate_pct = (
        Decimal(sum(pnl > 0 for pnl in closed_trade_pnls))
        / Decimal(closed_trade_count)
        * Decimal("100")
        if closed_trade_count
        else None
    )
    gross_profit = sum(
        (pnl for pnl in closed_trade_pnls if pnl > 0), start=Decimal("0")
    )
    gross_loss = sum(
        (-pnl for pnl in closed_trade_pnls if pnl < 0), start=Decimal("0")
    )
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    metrics = StrategyMetrics(
        initial_equity_usdt=config.initial_cash_usdt,
        final_equity_usdt=final_equity,
        gross_pnl_usdt=net_pnl + fees + slippage,
        total_fees_usdt=fees,
        total_slippage_cost_usdt=slippage,
        net_pnl_usdt=net_pnl,
        net_return_pct=net_pnl / config.initial_cash_usdt * Decimal("100"),
        max_drawdown_pct=_max_drawdown(equity, config.initial_cash_usdt),
        exposure_pct=(
            Decimal(sum(point.base_qty > 0 for point in equity))
            / Decimal(len(equity))
            * Decimal("100")
        ),
        turnover_quote_usdt=sum(
            (fill.quote_amount_usdt for fill in fills), start=Decimal("0")
        ),
        buy_count=sum(fill.side is Side.BUY for fill in fills),
        sell_count=sum(fill.side is Side.SELL for fill in fills),
        veto_count=sum(event.execution_status.startswith("VETOED") for event in signals),
        open_position=state.base_qty > 0,
        final_cash_usdt=state.cash_usdt,
        final_base_qty=state.base_qty,
        daily_sharpe=_daily_sharpe(daily_returns),
        daily_sharpe_observation_count=len(daily_returns),
        daily_sharpe_annualization_days=DAILY_SHARPE_ANNUALIZATION_DAYS,
        daily_sharpe_risk_free_daily_return=DAILY_SHARPE_RISK_FREE_RETURN,
        daily_sharpe_return_frequency=DAILY_SHARPE_RETURN_FREQUENCY,
        win_rate_pct=win_rate_pct,
        profit_factor=profit_factor,
        closed_trade_count=closed_trade_count,
    )
    return StrategyResult(
        strategy=strategy,
        signals=tuple(signals),
        fills=tuple(fills),
        equity=tuple(equity),
        metrics=metrics,
    )


def run_backtest(snapshot: BacktestSnapshot, config: BacktestConfig) -> BacktestResult:
    """Executa estratégia e benchmarks com um único ledger/fill model."""
    decision_candles, discarded_windows = _aggregate_decision_candles(
        snapshot.candles_1m, config.decision_timeframe
    )
    if not decision_candles:
        raise ValueError(
            f"snapshot {snapshot.snapshot_id} não contém janela completa de {config.decision_timeframe}"
        )

    minute_gaps = 0
    gap_count = 0
    gap_examples: list[dict[str, str]] = []
    for previous, current in zip(snapshot.candles_1m, snapshot.candles_1m[1:]):
        if current.open_at_utc != previous.end_at_utc:
            gap_count += 1
            missing_minutes = int(
                (current.open_at_utc - previous.end_at_utc).total_seconds() // 60
            )
            minute_gaps += max(missing_minutes, 1)
            if len(gap_examples) < 20:
                gap_examples.append(
                    {
                        "from_utc": previous.end_at_utc.isoformat().replace("+00:00", "Z"),
                        "to_utc": current.open_at_utc.isoformat().replace("+00:00", "Z"),
                        "missing_minutes": str(max(missing_minutes, 1)),
                    }
                )

    selected = config.strategy
    strategies = tuple(dict.fromkeys((selected, StrategyName.CASH, StrategyName.BUY_HOLD)))
    results: list[StrategyResult] = []
    warmup_by_strategy: dict[str, int] = {}
    for strategy in strategies:
        signals, warmup_discarded = _strategy_signals(
            strategy, decision_candles, snapshot.candles_1m[0], config
        )
        warmup_by_strategy[strategy.value] = warmup_discarded
        results.append(
            _simulate_strategy(snapshot.candles_1m, strategy, signals, config)
        )

    audit: dict[str, object] = {
        "qa_status": snapshot.qa_status,
        "missing_1m_minutes": minute_gaps,
        "gap_count": gap_count,
        "gap_examples": gap_examples,
        "gap_examples_omitted": max(gap_count - len(gap_examples), 0),
        "discarded_incomplete_decision_windows": discarded_windows,
        "warmup_windows_discarded": warmup_by_strategy,
        "vetoes_by_strategy": {
            result.strategy.value: result.metrics.veto_count for result in results
        },
        "open_positions_by_strategy": {
            result.strategy.value: result.metrics.open_position for result in results
        },
        "evidence_level": "DEMO" if snapshot.data_origin == "SYNTHETIC" else "IN_SAMPLE",
        "available_at_confidence": "ASSUMED",
        "snapshot_delay_assumed_ms": (snapshot.source_metadata or {}).get("delay_assumed_ms"),
        "backtest_delay_assumed_seconds": str(config.assumed_data_delay_seconds),
        "delay_overrides_snapshot": (
            (snapshot.source_metadata or {}).get("delay_assumed_ms") is not None
            and Decimal(str((snapshot.source_metadata or {})["delay_assumed_ms"]))
            != config.assumed_data_delay_seconds * Decimal("1000")
        ),
        "fill_assumption": "PROXY_OHLCV",
    }
    return BacktestResult(
        config=config,
        snapshot=snapshot,
        results=tuple(results),
        audit=audit,
    )


def run_backtest_window(
    snapshot: BacktestSnapshot,
    config: BacktestConfig,
    *,
    start_at_utc: datetime,
    end_at_utc: datetime,
    evidence_level: str = "WALK_FORWARD",
    target_overrides: Mapping[StrategyName, Mapping[datetime, Decimal | TargetExposure]] | None = None,
) -> BacktestResult:
    """Evaluate a window using the snapshot's earlier bars only as causal warm-up."""
    if start_at_utc.tzinfo is None or end_at_utc.tzinfo is None:
        raise ValueError("limites de avaliação devem conter timezone UTC explícito")
    start = start_at_utc.astimezone(UTC)
    end = end_at_utc.astimezone(UTC)
    if end <= start:
        raise ValueError("fim da janela deve ser posterior ao início")
    evaluation_candles = tuple(
        candle for candle in snapshot.candles_1m
        if candle.open_at_utc >= start and candle.end_at_utc <= end
    )
    if not evaluation_candles:
        raise ValueError("janela de avaliação sem candles 1m completos")

    overrides = {
        StrategyName(strategy): targets
        for strategy, targets in (target_overrides or {}).items()
    }
    unsupported = set(overrides) - EXTERNAL_TARGET_STRATEGIES
    if unsupported:
        raise ValueError("target_overrides aceita somente estratégias ML F6")

    decision_candles, discarded_windows = _aggregate_decision_candles(
        snapshot.candles_1m, config.decision_timeframe
    )
    strategy_signals, warmup_discarded = _strategy_signals(
        config.strategy, decision_candles, snapshot.candles_1m[0], config
    )
    if config.strategy in {StrategyName.CASH, StrategyName.BUY_HOLD}:
        strategy_signals, warmup_discarded = _strategy_signals(
            config.strategy, decision_candles, evaluation_candles[0], config
        )
    signals = [
        item for item in strategy_signals
        if item.generated_at_utc >= start and item.generated_at_utc < end
    ]
    scheduled_signals: list[SignalEvent] = []
    for signal in signals:
        if signal.eligible_at_utc >= end:
            signal.execution_status = "VETOED_EVALUATION_WINDOW_END"
            signal.veto_reason = "primeiro fill elegível está fora do período avaliado"
        else:
            scheduled_signals.append(signal)

    strategies = tuple(dict.fromkeys((
        config.strategy,
        StrategyName.CASH,
        StrategyName.BUY_HOLD,
        *overrides.keys(),
    )))
    results: list[StrategyResult] = []
    warmup_by_strategy: dict[str, int] = {}
    for strategy in strategies:
        if strategy == config.strategy:
            current_signals = signals
            warmup_by_strategy[strategy.value] = warmup_discarded
        elif strategy in overrides:
            current_signals = _external_target_signals(
                strategy,
                decision_candles,
                config,
                overrides[strategy],
            )
            current_signals = [
                item for item in current_signals
                if item.generated_at_utc >= start and item.generated_at_utc < end
            ]
            warmup_by_strategy[strategy.value] = 0
        else:
            current_signals, skipped = _strategy_signals(
                strategy, decision_candles, evaluation_candles[0], config
            )
            warmup_by_strategy[strategy.value] = skipped
        results.append(_simulate_strategy(evaluation_candles, strategy, current_signals, config))

    gap_count = 0
    missing_minutes = 0
    for previous, current in zip(evaluation_candles, evaluation_candles[1:]):
        if current.open_at_utc != previous.end_at_utc:
            gap_count += 1
            missing_minutes += max(int((current.open_at_utc - previous.end_at_utc).total_seconds() // 60), 1)
    audit: dict[str, object] = {
        "qa_status": snapshot.qa_status,
        "evidence_level": evidence_level,
        "evaluation_window": {
            "start_at_utc": start.isoformat().replace("+00:00", "Z"),
            "end_at_utc_exclusive": end.isoformat().replace("+00:00", "Z"),
            "candle_count": len(evaluation_candles),
            "window_execution": "flat_start; warm-up uses only earlier snapshot candles",
        },
        "missing_1m_minutes": missing_minutes,
        "gap_count": gap_count,
        "gap_examples": [],
        "gap_examples_omitted": 0,
        "discarded_incomplete_decision_windows": discarded_windows,
        "warmup_windows_discarded": warmup_by_strategy,
        "vetoes_by_strategy": {
            item.strategy.value: item.metrics.veto_count for item in results
        },
        "open_positions_by_strategy": {
            item.strategy.value: item.metrics.open_position for item in results
        },
        "available_at_confidence": "ASSUMED",
        "snapshot_delay_assumed_ms": (snapshot.source_metadata or {}).get("delay_assumed_ms"),
        "backtest_delay_assumed_seconds": str(config.assumed_data_delay_seconds),
        "fill_assumption": "PROXY_OHLCV",
    }
    return BacktestResult(config=config, snapshot=snapshot, results=tuple(results), audit=audit)
