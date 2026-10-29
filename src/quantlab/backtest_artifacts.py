"""Persistência e relatório local para runs auditáveis da fase F3."""

from __future__ import annotations

import csv
import hashlib
import html
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from uuid import uuid4

from quantlab.backtest import (
    BacktestResult,
    StrategyResult,
    closed_trade_pnl_by_exit_timestamp,
)
from quantlab.domain import Side


def _decimal(value: Decimal) -> str:
    return format(value, "f")


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat().replace("+00:00", "Z")


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"


def _percent(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}%"


def _optional_decimal(value: Decimal | None, places: str = "0.0000") -> str:
    if value is None:
        return "N/D"
    return format(value.quantize(Decimal(places), rounding=ROUND_HALF_UP), "f")


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _git_revision() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "UNCOMMITTED"
    return result.stdout.strip() or "UNCOMMITTED"


def _lock_hash() -> str:
    path = Path.cwd() / "uv.lock"
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "UNAVAILABLE"


def _config_dict(result: BacktestResult) -> dict[str, object]:
    config = result.config
    return {
        "strategy": config.strategy.value,
        "strategy_version": "F3-v2",
        "decision_timeframe": config.decision_timeframe,
        "initial_cash_usdt": _decimal(config.initial_cash_usdt),
        "fee_bps_per_side": _decimal(config.fee_bps_per_side),
        "slippage_bps_per_side": _decimal(config.slippage_bps_per_side),
        "assumed_data_delay_seconds": _decimal(config.assumed_data_delay_seconds),
        "assumed_data_delay_ms": _decimal(config.assumed_data_delay_seconds * Decimal("1000")),
        "sma_fast": config.sma_fast,
        "sma_slow": config.sma_slow,
        "breakout_lookback": config.breakout_lookback,
        "momentum_lookback": config.momentum_lookback,
        "mean_reversion_lookback": config.mean_reversion_lookback,
        "mean_reversion_entry_z": _decimal(config.mean_reversion_entry_z),
        "mean_reversion_exit_z": _decimal(config.mean_reversion_exit_z),
        "sizing_base_qty_step": "0.00000001",
    }


def _canonical_config_hash(config: dict[str, object]) -> str:
    content = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _metrics_dict(result: StrategyResult) -> dict[str, object]:
    metrics = result.metrics
    return {
        "initial_equity_usdt": _decimal(metrics.initial_equity_usdt),
        "final_equity_usdt": _decimal(metrics.final_equity_usdt),
        "gross_pnl_usdt": _decimal(metrics.gross_pnl_usdt),
        "total_fees_usdt": _decimal(metrics.total_fees_usdt),
        "total_slippage_cost_usdt": _decimal(metrics.total_slippage_cost_usdt),
        "net_pnl_usdt": _decimal(metrics.net_pnl_usdt),
        "net_return_pct": _decimal(metrics.net_return_pct),
        "max_drawdown_pct": _decimal(metrics.max_drawdown_pct),
        "exposure_pct": _decimal(metrics.exposure_pct),
        "turnover_quote_usdt": _decimal(metrics.turnover_quote_usdt),
        "buy_count": metrics.buy_count,
        "sell_count": metrics.sell_count,
        "veto_count": metrics.veto_count,
        "open_position": metrics.open_position,
        "final_cash_usdt": _decimal(metrics.final_cash_usdt),
        "final_base_qty": _decimal(metrics.final_base_qty),
        "daily_sharpe": (
            _decimal(metrics.daily_sharpe)
            if metrics.daily_sharpe is not None
            else None
        ),
        "daily_sharpe_observation_count": metrics.daily_sharpe_observation_count,
        "daily_sharpe_annualization_days": metrics.daily_sharpe_annualization_days,
        "daily_sharpe_risk_free_daily_return": _decimal(
            metrics.daily_sharpe_risk_free_daily_return
        ),
        "daily_sharpe_return_frequency": metrics.daily_sharpe_return_frequency,
        "win_rate_pct": (
            _decimal(metrics.win_rate_pct) if metrics.win_rate_pct is not None else None
        ),
        "profit_factor": (
            _decimal(metrics.profit_factor) if metrics.profit_factor is not None else None
        ),
        "closed_trade_count": metrics.closed_trade_count,
    }


def _write_signals(path: Path, results: tuple[StrategyResult, ...]) -> None:
    columns = (
        "strategy",
        "generated_at_utc",
        "available_at_utc",
        "eligible_at_utc",
        "execution_at_utc",
        "target_exposure",
        "reason",
        "execution_status",
        "veto_reason",
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for result in results:
            for event in result.signals:
                writer.writerow(
                    {
                        "strategy": result.strategy.value,
                        "generated_at_utc": _iso(event.generated_at_utc),
                        "available_at_utc": _iso(event.available_at_utc),
                        "eligible_at_utc": _iso(event.eligible_at_utc),
                        "execution_at_utc": _iso(event.execution_at_utc),
                        "target_exposure": _decimal(event.target_exposure),
                        "reason": event.reason,
                        "execution_status": event.execution_status,
                        "veto_reason": event.veto_reason or "",
                    }
                )


def _write_fills(path: Path, results: tuple[StrategyResult, ...]) -> None:
    columns = (
        "strategy",
        "timestamp_utc",
        "side",
        "quantity_base",
        "reference_open_usdt",
        "execution_price_usdt",
        "quote_amount_usdt",
        "slippage_cost_usdt",
        "fee_usdt",
        "realized_pnl_usdt",
        "fee_currency",
        "cash_after_usdt",
        "base_qty_after",
        "fill_assumption",
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for result in results:
            realized_by_exit = closed_trade_pnl_by_exit_timestamp(result.fills)
            for fill in result.fills:
                writer.writerow(
                    {
                        "strategy": result.strategy.value,
                        "timestamp_utc": _iso(fill.timestamp_utc),
                        "side": fill.side.value,
                        "quantity_base": _decimal(fill.quantity_base),
                        "reference_open_usdt": _decimal(fill.reference_price_usdt),
                        "execution_price_usdt": _decimal(fill.price_usdt),
                        "quote_amount_usdt": _decimal(fill.quote_amount_usdt),
                        "slippage_cost_usdt": _decimal(fill.slippage_cost_usdt),
                        "fee_usdt": _decimal(fill.fee_usdt),
                        "realized_pnl_usdt": (
                            _decimal(realized_by_exit[fill.timestamp_utc])
                            if fill.side is Side.SELL
                            else ""
                        ),
                        "fee_currency": fill.fee_currency,
                        "cash_after_usdt": _decimal(fill.cash_after_usdt),
                        "base_qty_after": _decimal(fill.base_qty_after),
                        "fill_assumption": "PROXY_OHLCV",
                    }
                )


def _write_equity(path: Path, results: tuple[StrategyResult, ...]) -> None:
    columns = (
        "strategy",
        "timestamp_utc",
        "cash_usdt",
        "base_qty",
        "mark_price_usdt",
        "equity_usdt",
        "fees_paid_usdt",
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for result in results:
            for point in result.equity:
                writer.writerow(
                    {
                        "strategy": result.strategy.value,
                        "timestamp_utc": _iso(point.timestamp_utc),
                        "cash_usdt": _decimal(point.cash_usdt),
                        "base_qty": _decimal(point.base_qty),
                        "mark_price_usdt": _decimal(point.mark_price_usdt),
                        "equity_usdt": _decimal(point.equity_usdt),
                        "fees_paid_usdt": _decimal(point.fees_paid_usdt),
                    }
                )


def _downsample(result: StrategyResult, limit: int = 500) -> list[tuple[int, Decimal]]:
    points = result.equity
    if len(points) <= limit:
        return [(index, item.equity_usdt) for index, item in enumerate(points)]
    stride = (len(points) - 1) / (limit - 1)
    indexes = sorted({round(index * stride) for index in range(limit)} | {len(points) - 1})
    return [(index, points[index].equity_usdt) for index in indexes]


def _chart_svg(results: tuple[StrategyResult, ...]) -> str:
    palette = {
        "CASH": "#64748b",
        "BUY_HOLD": "#9333ea",
        "SMA_CROSS": "#047857",
        "BREAKOUT": "#b45309",
        "MOMENTUM": "#0891b2",
        "MEAN_REVERSION": "#db2777",
    }
    all_values = [point.equity_usdt for result in results for point in result.equity]
    low, high = min(all_values), max(all_values)
    if low == high:
        padding = max(abs(high) * Decimal("0.02"), Decimal("1"))
    else:
        padding = (high - low) * Decimal("0.08")
    low -= padding
    high += padding
    width, height = 920, 300
    left, right, top, bottom = 74, 900, 20, 252
    shapes: list[str] = []
    for result in results:
        points = _downsample(result)
        coordinates = []
        for index, value in points:
            x = left + index * (right - left) / max(len(result.equity) - 1, 1)
            y = bottom - float(value - low) * (bottom - top) / float(high - low)
            coordinates.append(f"{x:.2f},{y:.2f}")
        color = palette.get(result.strategy.value, "#2563eb")
        shapes.append(
            f'<polyline fill="none" stroke="{color}" stroke-width="2" '
            f'stroke-linejoin="round" points="{" ".join(coordinates)}">'
            f"<title>{html.escape(result.strategy.value)}</title></polyline>"
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Equity marcada em USDT">'
        f'<text x="4" y="24" class="axis">{_money(high)}</text>'
        f'<text x="4" y="{bottom}" class="axis">{_money(low)}</text>'
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" stroke="#cbd5e1"/>'
        f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" stroke="#cbd5e1"/>'
        f"{''.join(shapes)}</svg>"
    )


def _render_report(
    run_id: str,
    result: BacktestResult,
    evidence_level: str,
) -> str:
    first = result.snapshot.candles_1m[0]
    last = result.snapshot.candles_1m[-1]
    evidence_label = (
        "DADOS SINTÉTICOS · DEMO"
        if result.snapshot.data_origin == "SYNTHETIC"
        else "DADOS HISTÓRICOS · IN_SAMPLE"
    )
    metric_rows = []
    for item in result.results:
        metrics = item.metrics
        metric_rows.append(
            "<tr>"
            f"<th scope=\"row\">{html.escape(item.strategy.value)}</th>"
            f"<td>{_money(metrics.initial_equity_usdt)} USDT</td>"
            f"<td>{_money(metrics.final_equity_usdt)} USDT</td>"
            f"<td>{_money(metrics.gross_pnl_usdt)} USDT</td>"
            f"<td>{_money(metrics.total_fees_usdt)} USDT</td>"
            f"<td>{_money(metrics.total_slippage_cost_usdt)} USDT</td>"
            f"<td>{_money(metrics.net_pnl_usdt)} USDT</td>"
            f"<td>{_percent(metrics.net_return_pct)}</td>"
            f"<td>{_percent(metrics.max_drawdown_pct)}</td>"
            f"<td>{_percent(metrics.exposure_pct)}</td>"
            f"<td>{_optional_decimal(metrics.daily_sharpe)}</td>"
            f"<td>{metrics.daily_sharpe_observation_count}</td>"
            f"<td>{_percent(metrics.win_rate_pct) if metrics.win_rate_pct is not None else 'N/D'}</td>"
            f"<td>{_optional_decimal(metrics.profit_factor)}</td>"
            f"<td>{metrics.veto_count}</td>"
            f"<td>{'SIM' if metrics.open_position else 'NÃO'}</td>"
            "</tr>"
        )

    sample_signals = [
        (item.strategy.value, event)
        for item in result.results
        for event in item.signals
        if item.strategy is result.config.strategy
    ][:20]
    signal_rows = [
        "<tr>"
        f"<td>{html.escape(strategy)}</td>"
        f"<td>{html.escape(_iso(event.generated_at_utc))}</td>"
        f"<td>{html.escape(_iso(event.available_at_utc))}</td>"
        f"<td>{html.escape(_iso(event.eligible_at_utc))}</td>"
        f"<td>{html.escape(_iso(event.execution_at_utc) or '—')}</td>"
        f"<td>{_decimal(event.target_exposure)}</td>"
        f"<td>{html.escape(event.execution_status)}</td>"
        f"<td>{html.escape(event.reason)}</td>"
        "</tr>"
        for strategy, event in sample_signals
    ]
    if not signal_rows:
        signal_rows.append('<tr><td colspan="8">Nenhum sinal após o warm-up e QA.</td></tr>')
    strategy_result = next(
        (item for item in result.results if item.strategy is result.config.strategy),
        None,
    )
    realized_by_exit = (
        closed_trade_pnl_by_exit_timestamp(strategy_result.fills)
        if strategy_result is not None
        else {}
    )
    fill_rows = [
        "<tr>"
        f"<td>{html.escape(fill.timestamp_utc.isoformat().replace('+00:00', 'Z'))}</td>"
        f"<td>{html.escape(fill.side.value)}</td>"
        f"<td>{_decimal(fill.quantity_base)}</td>"
        f"<td>{_money(fill.reference_price_usdt)} USDT</td>"
        f"<td>{_money(fill.price_usdt)} USDT</td>"
        f"<td>{_money(fill.fee_usdt)} USDT</td>"
        f"<td>{_money(realized_by_exit[fill.timestamp_utc]) if fill.timestamp_utc in realized_by_exit else '—'} USDT</td>"
        f"<td>{_money(fill.cash_after_usdt)} USDT</td>"
        f"<td>{_decimal(fill.base_qty_after)}</td>"
        "</tr>"
        for fill in (strategy_result.fills[:20] if strategy_result else ())
    ]
    if not fill_rows:
        fill_rows.append('<tr><td colspan="9">Nenhum fill na estratégia selecionada.</td></tr>')

    gap_examples = result.audit.get("gap_examples", [])
    gap_items = "".join(
        "<li>"
        f"{html.escape(str(item.get('from_utc', '')))} → "
        f"{html.escape(str(item.get('to_utc', '')))}: "
        f"{html.escape(str(item.get('missing_minutes', '')))} minuto(s) ausente(s)"
        "</li>"
        for item in gap_examples
    ) or "<li>Nenhuma lacuna interna encontrada.</li>"
    open_rows = "".join(
        "<li>"
        f"{html.escape(item.strategy.value)}: caixa {_money(item.metrics.final_cash_usdt)} USDT; "
        f"posição base {_decimal(item.metrics.final_base_qty)}; "
        f"posição aberta: {'sim' if item.metrics.open_position else 'não'}"
        "</li>"
        for item in result.results
    )
    source_delay_ms = result.audit.get("snapshot_delay_assumed_ms")
    warmup_windows = result.audit.get("warmup_windows_discarded", {})
    warmup_summary = ", ".join(
        f"{html.escape(str(strategy))}: {html.escape(str(count))}"
        for strategy, count in warmup_windows.items()
    ) or "sem janelas de warm-up descartadas"
    delay_audit = (
        f"Snapshot F2: {html.escape(str(source_delay_ms))} ms; "
        f"política deste run: {_decimal(result.config.assumed_data_delay_seconds * Decimal('1000'))} ms; "
        f"override explícito: {'sim' if result.audit.get('delay_overrides_snapshot') else 'não'}."
        if source_delay_ms is not None
        else "Delay registrado na configuração do run; não recebido do manifesto do snapshot."
    )

    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>QuantLab F3 — {html.escape(run_id)}</title>
<style>
:root {{ color-scheme: light; font-family: system-ui, Segoe UI, sans-serif; color: #17202a; background: #f5f7f8; }}
body {{ max-width: 1240px; margin: 0 auto; padding: 24px; }}
header, section {{ background: white; border: 1px solid #dce3e6; border-radius: 12px; padding: 20px; margin-bottom: 16px; }}
header {{ border-top: 5px solid #047857; }}
.badge {{ display: inline-block; background: #dcfce7; color: #14532d; border: 1px solid #86efac; border-radius: 999px; padding: 5px 11px; font-weight: 800; }}
.warning {{ color: #713f12; background: #fffbeb; padding: 12px; border-radius: 8px; }}
.small {{ color: #475569; font-size: .92rem; overflow-wrap: anywhere; }}
.table-wrap {{ overflow-x: auto; }}
table {{ width: 100%; border-collapse: collapse; font-size: .88rem; }}
th, td {{ border-bottom: 1px solid #e5e7eb; text-align: left; padding: 8px; white-space: nowrap; }}
svg {{ display: block; width: 100%; max-height: 380px; }} .axis {{ fill: #64748b; font-size: 12px; }}
a {{ color: #075985; }} code {{ overflow-wrap: anywhere; }}
@media (max-width: 640px) {{ body {{ padding: 12px; }} header, section {{ padding: 14px; }} }}
</style></head><body>
<header>
  <span class="badge">{html.escape(evidence_label)} · EVIDÊNCIA {html.escape(evidence_level)}</span>
  <h1>Backtest spot long-only</h1>
  <p class="warning">OHLCV é aproximação de fill. <code>available_at</code> histórico é hipótese de disponibilidade com atraso assumido, não latência medida. Fee e slippage são hipóteses configuradas, não taxas oficiais Binance. Resultado não garante rentabilidade.</p>
  <p><strong>RUN_ID:</strong> <code>{html.escape(run_id)}</code></p>
  <p><strong>Snapshot:</strong> <code>{html.escape(result.snapshot.snapshot_id)}</code> · {html.escape(result.snapshot.instrument_id)} · QA {html.escape(result.snapshot.qa_status)} · {html.escape(result.snapshot.data_origin)}</p>
  <p><strong>Recorte UTC:</strong> {_iso(first.open_at_utc)} — {_iso(last.end_at_utc)} · decisões {html.escape(result.config.decision_timeframe)} · estratégia {html.escape(result.config.strategy.value)}</p>
  <p><strong>Política:</strong> atraso assumido {_decimal(result.config.assumed_data_delay_seconds * Decimal("1000"))} ms · primeiro open 1m estritamente posterior · fee {_decimal(result.config.fee_bps_per_side)} bps/lado em USDT · proxy de slippage {_decimal(result.config.slippage_bps_per_side)} bps/lado.</p>
  <p class="small">{delay_audit} <code>available_at</code> continua sendo ASSUMED.</p>
  <p class="small">Slippage é custo adicional aplicado ao open de referência; não há spread somado separadamente. Fill integral de mercado simulado ou veto; sem short, margem, alavancagem, ordem real ou venda forçada no fim.</p>
</header>
<section><h2>Comparação sob a mesma política</h2><div class="table-wrap"><table>
<thead><tr><th>Estratégia</th><th>Inicial</th><th>Final</th><th>P&amp;L bruto</th><th>Fees</th><th>Slippage proxy</th><th>P&amp;L líquido</th><th>Retorno</th><th>Drawdown máx.</th><th>Exposição</th><th>Sharpe diário</th><th>Obs. diárias</th><th>Win rate</th><th>Profit factor</th><th>Vetos</th><th>Posição aberta</th></tr></thead>
<tbody>{''.join(metric_rows)}</tbody></table></div></section>
<p class="small">Sharpe usa retornos simples UTC de dias completos consecutivos, rf diário 0 e anualização √365; fica N/D com menos de duas observações ou variância zero. Win rate e profit factor consideram somente trades fechados e custos líquidos; profit factor fica N/D sem perdas brutas.</p>
<section><h2>Equity marcada no close 1m</h2><p class="small">Equity = caixa USDT + quantidade base × close 1m. A curva SVG é amostrada para exibição; o arquivo equity.csv mantém todas as marcações.</p>{_chart_svg(result.results)}</section>
<section><h2>Gaps e janelas descartadas</h2>
<p>QA snapshot: <strong>{html.escape(str(result.audit.get('qa_status', 'UNKNOWN')))}</strong> · intervalos de gap: <strong>{html.escape(str(result.audit.get('gap_count', 0)))}</strong> · minutos ausentes: <strong>{html.escape(str(result.audit.get('missing_1m_minutes', 0)))}</strong> · exemplos omitidos: <strong>{html.escape(str(result.audit.get('gap_examples_omitted', 0)))}</strong> · janelas {html.escape(result.config.decision_timeframe)} incompletas descartadas: <strong>{html.escape(str(result.audit.get('discarded_incomplete_decision_windows', 0)))}</strong></p>
<ul>{gap_items}</ul><p class="small">Decisões exigem janelas completas; o warm-up é descartado e recomeça depois de uma lacuna. Janelas descartadas no warm-up por estratégia: {warmup_summary}.</p></section>
<section><h2>Vetos e posições finais</h2><ul>{open_rows}</ul><p class="small">Uma decisão sem open elegível, com gap no minuto anterior ou sem caixa suficiente é registrada como veto em signals.csv.</p></section>
<section><h2>Sinais da estratégia</h2><p class="small">Prévia das primeiras 20 linhas. O CSV contém o histórico completo, inclusive horários de decisão, disponibilidade, elegibilidade, execução e vetos.</p>
<div class="table-wrap"><table><thead><tr><th>Estratégia</th><th>Gerado UTC</th><th>Available at UTC</th><th>Open elegível UTC</th><th>Execução UTC</th><th>Target</th><th>Status</th><th>Motivo</th></tr></thead><tbody>{''.join(signal_rows)}</tbody></table></div></section>
<section><h2>Fills da estratégia</h2><p class="small">Prévia das primeiras 20 execuções. Preço de referência é o open 1m; preço executado inclui somente a proxy de slippage configurada. A fee quote é debitada separadamente.</p>
<div class="table-wrap"><table><thead><tr><th>Timestamp UTC</th><th>Lado</th><th>Quantidade base</th><th>Open de referência</th><th>Preço executado</th><th>Fee</th><th>P&amp;L realizado</th><th>Caixa após</th><th>Base após</th></tr></thead><tbody>{''.join(fill_rows)}</tbody></table></div></section>
<section><h2>Arquivos do run</h2><ul>
<li><a href="run.json">run.json</a> — configuração e hashes exatos</li><li><a href="sources.json">sources.json</a> — snapshot e proveniência</li><li><a href="strategy.json">strategy.json</a> — regra e parâmetros</li><li><a href="execution_policy.json">execution_policy.json</a> — relógio e custos</li><li><a href="signals.csv">signals.csv</a></li><li><a href="fills.csv">fills.csv</a></li><li><a href="equity.csv">equity.csv</a></li><li><a href="metrics.json">metrics.json</a></li><li><a href="audit.json">audit.json</a></li>
</ul><p class="small">BUY_HOLD e CASH usam a mesma política de disponibilidade, fill, fee e slippage. A posição que permanecer aberta ao fim é marcada no close, sem liquidação silenciosa.</p></section>
</body></html>"""


def write_backtest_artifacts(output_root: Path, result: BacktestResult) -> dict[str, Path]:
    """Grava todos os artefatos e publica o diretório atomicamente."""
    created_at = datetime.now(UTC)
    run_id = f"{created_at:%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    final_dir = output_root / run_id
    staging_dir = output_root / f".{run_id}.tmp"
    staging_dir.mkdir()

    config = _config_dict(result)
    config_hash = _canonical_config_hash(config)
    evidence_level = str(
        result.audit.get(
            "evidence_level",
            "DEMO" if result.snapshot.data_origin == "SYNTHETIC" else "IN_SAMPLE",
        )
    )
    evaluation_window = result.audit.get("evaluation_window")
    start_open = (
        str(evaluation_window["start_at_utc"])
        if isinstance(evaluation_window, dict) and evaluation_window.get("start_at_utc")
        else _iso(result.snapshot.candles_1m[0].open_at_utc)
    )
    end_exclusive = (
        str(evaluation_window["end_at_utc_exclusive"])
        if isinstance(evaluation_window, dict) and evaluation_window.get("end_at_utc_exclusive")
        else _iso(result.snapshot.candles_1m[-1].end_at_utc)
    )
    snapshot = {
        "snapshot_id": result.snapshot.snapshot_id,
        "instrument_id": result.snapshot.instrument_id,
        "content_sha256": result.snapshot.content_sha256,
        "qa_status": result.snapshot.qa_status,
        "data_origin": result.snapshot.data_origin,
        "source_metadata": dict(result.snapshot.source_metadata or {}),
        "snapshot_manifest": dict(result.snapshot.source_manifest or {}),
        "start_open_at_utc": start_open,
        "end_at_utc": end_exclusive,
        "candle_count": (
            int(evaluation_window.get("candle_count", len(result.snapshot.candles_1m)))
            if isinstance(evaluation_window, dict)
            else len(result.snapshot.candles_1m)
        ),
        "base_interval": "1m",
    }
    fee_basis_points = _decimal(result.config.fee_bps_per_side)
    slip_basis_points = _decimal(result.config.slippage_bps_per_side)
    execution_policy = {
        "market": "SPOT",
        "position_mode": "LONG_ONLY",
        "quote_currency": "USDT",
        "allow_short": False,
        "allow_leverage": False,
        "assumed_data_delay_seconds": _decimal(result.config.assumed_data_delay_seconds),
        "assumed_data_delay_ms": _decimal(result.config.assumed_data_delay_seconds * Decimal("1000")),
        "available_at_confidence": "ASSUMED",
        "fill_on": "first_1m_open_strictly_after_available_at",
        "fill_assumption": "PROXY_OHLCV",
        "reference_price": "execution_1m_open",
        "slippage_proxy_bps_per_side": slip_basis_points,
        "spread_added_separately": False,
        "fee_bps_per_side": fee_basis_points,
        "fee_rate_per_side": _decimal(result.config.fee_bps_per_side / Decimal("10000")),
        "fee_currency": "USDT",
        "end_position": "MARKED_NOT_FORCE_SOLD",
        "partial_fills": False,
        "margin": False,
    }
    strategy_spec = {
        "name": result.config.strategy.value,
        "version": "F3-v1",
        "decision_timeframe": result.config.decision_timeframe,
        "parameters": {
            "sma_fast": result.config.sma_fast,
            "sma_slow": result.config.sma_slow,
            "breakout_lookback": result.config.breakout_lookback,
            "breakout_exit": "zera exposição quando close não rompe a máxima anterior",
            "momentum_lookback": result.config.momentum_lookback,
            "mean_reversion_lookback": result.config.mean_reversion_lookback,
            "mean_reversion_entry_z": _decimal(result.config.mean_reversion_entry_z),
            "mean_reversion_exit_z": _decimal(result.config.mean_reversion_exit_z),
            "mean_reversion_z_score": (
                "z-score trailing sobre fechamentos; janela inclui a barra atual; "
                "desvio padrão populacional; long-only com histerese"
            ),
        },
        "warmup": "sem sinal até janela completa; reinicia após lacuna",
    }
    config_hashes = {"config_sha256": config_hash, "snapshot_sha256": snapshot["content_sha256"]}
    run_manifest = {
        "artifact_schema_version": 1,
        "phase": str(result.audit.get("phase") or (
            "F5_HOLDOUT" if evidence_level == "SEALED_HOLDOUT" else "F3"
        )),
        "run_id": run_id,
        "created_at_utc": _iso(created_at),
        "evidence_level": evidence_level,
        "data_origin": result.snapshot.data_origin,
        "snapshot": snapshot,
        "strategy": strategy_spec,
        "config": config,
        "config_hashes": config_hashes,
        "execution_policy": execution_policy,
        "runtime": {"python": sys.version.split()[0]},
        "git_sha": _git_revision(),
        "uv_lock_sha256": _lock_hash(),
        "limitations": [
            "available_at histórico é hipótese com atraso assumido, não latência observada",
            "OHLCV é aproximação; fill integral usa open 1m e slippage configurado",
            "fee e slippage são hipóteses, não taxas oficiais da Binance",
            "resultado IN_SAMPLE é exploratório e não demonstra rentabilidade futura",
        ],
        "file_formats": {"signals": "CSV", "fills": "CSV", "equity": "CSV"},
    }
    if isinstance(evaluation_window, dict):
        run_manifest["evaluation_window"] = evaluation_window
        run_manifest["research_context"] = dict(result.audit.get("research_context") or {})
        run_manifest["limitations"].append(
            "a carteira reinicia em caixa no início do recorte; barras anteriores servem somente para warm-up causal"
        )
    if isinstance(result.audit.get("ml_context"), dict):
        run_manifest["ml_context"] = dict(result.audit["ml_context"])
    metrics = {
        "evidence_level": evidence_level,
        "data_origin": result.snapshot.data_origin,
        "config_sha256": config_hash,
        "strategies": {
            item.strategy.value: _metrics_dict(item) for item in result.results
        },
    }

    try:
        _write_json(staging_dir / "run.json", run_manifest)
        _write_json(staging_dir / "sources.json", snapshot)
        _write_json(staging_dir / "strategy.json", strategy_spec)
        _write_json(staging_dir / "execution_policy.json", execution_policy)
        _write_json(staging_dir / "metrics.json", metrics)
        _write_json(staging_dir / "audit.json", dict(result.audit))
        _write_signals(staging_dir / "signals.csv", result.results)
        _write_fills(staging_dir / "fills.csv", result.results)
        _write_equity(staging_dir / "equity.csv", result.results)
        report = _render_report(run_id, result, evidence_level)
        (staging_dir / "report.html").write_text(report, encoding="utf-8")
        staging_dir.rename(final_dir)
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    return {
        "run_id": run_id,
        "directory": final_dir,
        "run_json": final_dir / "run.json",
        "signals_csv": final_dir / "signals.csv",
        "fills_csv": final_dir / "fills.csv",
        "equity_csv": final_dir / "equity.csv",
        "metrics_json": final_dir / "metrics.json",
        "report_html": final_dir / "report.html",
    }
