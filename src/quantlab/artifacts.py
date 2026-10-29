"""Grava runs locais e um relatório HTML que não depende de rede."""

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

from quantlab.domain import Candle, RunArtifacts, StrategyName
from quantlab.engine import BacktestResult
from quantlab.fixtures import SYNTHETIC_DATASET_ID, SYNTHETIC_NOTICE


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"


def _percent(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}%"


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _fixture_sha256(candles: list[Candle]) -> str:
    lines = [
        "|".join(
            (
                _iso(candle.open_at_utc),
                _decimal_text(candle.open),
                _decimal_text(candle.high),
                _decimal_text(candle.low),
                _decimal_text(candle.close),
                _decimal_text(candle.volume_base),
            )
        )
        for candle in candles
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _lock_sha256() -> str:
    lock_path = Path.cwd() / "uv.lock"
    if not lock_path.is_file():
        return "UNAVAILABLE"
    return hashlib.sha256(lock_path.read_bytes()).hexdigest()


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


def _metrics_json(result: BacktestResult) -> dict[str, str | int]:
    metrics = result.metrics
    return {
        "initial_equity_usdt": _decimal_text(metrics.initial_equity_usdt),
        "final_equity_usdt": _decimal_text(metrics.final_equity_usdt),
        "net_pnl_usdt": _decimal_text(metrics.net_pnl_usdt),
        "net_return_pct": _decimal_text(metrics.net_return_pct),
        "total_fees_usdt": _decimal_text(metrics.total_fees_usdt),
        "total_slippage_cost_usdt": _decimal_text(metrics.total_slippage_cost_usdt),
        "max_drawdown_pct": _decimal_text(metrics.max_drawdown_pct),
        "exposure_pct": _decimal_text(metrics.exposure_pct),
        "fill_count": metrics.fill_count,
        "final_cash_usdt": _decimal_text(metrics.final_cash_usdt),
        "final_base_qty": _decimal_text(metrics.final_base_qty),
    }


def _write_json(path: Path, data: dict) -> None:
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _write_fills(path: Path, results: list[BacktestResult]) -> None:
    columns = (
        "strategy",
        "timestamp_utc",
        "side",
        "quantity_base",
        "reference_price_usdt",
        "price_usdt",
        "quote_amount_usdt",
        "slippage_cost_usdt",
        "fee_usdt",
        "fee_currency",
        "cash_after_usdt",
        "base_qty_after",
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for result in results:
            for fill in result.fills:
                writer.writerow(
                    {
                        "strategy": result.strategy.value,
                        "timestamp_utc": _iso(fill.timestamp_utc),
                        "side": fill.side.value,
                        "quantity_base": _decimal_text(fill.quantity_base),
                        "reference_price_usdt": _decimal_text(fill.reference_price_usdt),
                        "price_usdt": _decimal_text(fill.price_usdt),
                        "quote_amount_usdt": _decimal_text(fill.quote_amount_usdt),
                        "slippage_cost_usdt": _decimal_text(fill.slippage_cost_usdt),
                        "fee_usdt": _decimal_text(fill.fee_usdt),
                        "fee_currency": fill.fee_currency,
                        "cash_after_usdt": _decimal_text(fill.cash_after_usdt),
                        "base_qty_after": _decimal_text(fill.base_qty_after),
                    }
                )


def _write_equity(path: Path, results: list[BacktestResult]) -> None:
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
                        "strategy": point.strategy.value,
                        "timestamp_utc": _iso(point.timestamp_utc),
                        "cash_usdt": _decimal_text(point.cash_usdt),
                        "base_qty": _decimal_text(point.base_qty),
                        "mark_price_usdt": _decimal_text(point.mark_price_usdt),
                        "equity_usdt": _decimal_text(point.equity_usdt),
                        "fees_paid_usdt": _decimal_text(point.fees_paid_usdt),
                    }
                )


def _chart_svg(results: list[BacktestResult]) -> str:
    values = [float(point.equity_usdt) for result in results for point in result.equity]
    low, high = min(values), max(values)
    span = high - low
    if span == 0:
        span = max(abs(high) * 0.02, 1.0)
        low -= span / 2
        high += span / 2
    else:
        padding = span * 0.08
        low -= padding
        high += padding

    width, height = 760, 250
    left, right, top, bottom = 54, 740, 18, 212
    lines = []
    for result, color in zip(results, ("#0f766e", "#a855f7"), strict=False):
        points = result.equity
        coords = []
        for index, point in enumerate(points):
            x = left + index * (right - left) / max(len(points) - 1, 1)
            y = bottom - (float(point.equity_usdt) - low) * (bottom - top) / (high - low)
            coords.append(f"{x:.2f},{y:.2f}")
        lines.append(
            f'<polyline fill="none" stroke="{color}" stroke-width="2.5" '
            f'stroke-linecap="round" stroke-linejoin="round" points="{" ".join(coords)}">'
            f"<title>{html.escape(result.strategy.value)}</title></polyline>"
        )
    baseline_y = bottom - (1000.0 - low) * (bottom - top) / (high - low)
    baseline = ""
    if top <= baseline_y <= bottom:
        baseline = (
            f'<line x1="{left}" y1="{baseline_y:.2f}" x2="{right}" '
            f'y2="{baseline_y:.2f}" stroke="#9ca3af" stroke-dasharray="5 5"/>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-labelledby="chart-title chart-desc">'
        "<title id=\"chart-title\">Equity sintética em USDT</title>"
        "<desc id=\"chart-desc\">Comparação das curvas CASH e BUY_HOLD; dados sintéticos.</desc>"
        f'<text x="6" y="20" class="axis">{_money(Decimal(str(high)))}</text>'
        f'<text x="6" y="{bottom}" class="axis">{_money(Decimal(str(low)))}</text>'
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" stroke="#d1d5db"/>'
        f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" stroke="#d1d5db"/>'
        f"{baseline}{''.join(lines)}</svg>"
    )


def _render_report(
    run_id: str,
    created_at: datetime,
    candles: list[Candle],
    results: list[BacktestResult],
    fixture_hash: str,
) -> str:
    summary_rows = []
    for result in results:
        metrics = result.metrics
        summary_rows.append(
            "<tr>"
            f"<th scope=\"row\">{html.escape(result.strategy.value)}</th>"
            f"<td>{_money(metrics.initial_equity_usdt)} USDT</td>"
            f"<td>{_money(metrics.final_equity_usdt)} USDT</td>"
            f"<td>{_money(metrics.net_pnl_usdt)} USDT</td>"
            f"<td>{_percent(metrics.net_return_pct)}</td>"
            f"<td>{_money(metrics.total_fees_usdt)} USDT</td>"
            f"<td>{_money(metrics.final_cash_usdt)} USDT</td>"
            f"<td>{_decimal_text(metrics.final_base_qty)}</td>"
            "</tr>"
        )

    fill_rows = []
    for result in results:
        for fill in result.fills:
            fill_rows.append(
                "<tr>"
                f"<td>{html.escape(result.strategy.value)}</td>"
                f"<td>{html.escape(_iso(fill.timestamp_utc))}</td>"
                f"<td>{html.escape(fill.side.value)}</td>"
                f"<td>{_decimal_text(fill.quantity_base)}</td>"
                f"<td>{_money(fill.price_usdt)} USDT</td>"
                f"<td>{_money(fill.fee_usdt)} USDT</td>"
                f"<td>{_money(fill.cash_after_usdt)} USDT</td>"
                f"<td>{_decimal_text(fill.base_qty_after)}</td>"
                "</tr>"
            )
    if not fill_rows:
        fill_rows.append('<tr><td colspan="8">Nenhum fill: a estratégia permaneceu em caixa.</td></tr>')

    strategies = ", ".join(result.strategy.value for result in results)
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>QuantLab — demo {html.escape(run_id)}</title>
  <style>
    :root {{ color-scheme: light; font-family: system-ui, Segoe UI, sans-serif; color: #17202a; background: #f5f7f8; }}
    body {{ max-width: 1120px; margin: 0 auto; padding: 24px; }}
    header, section {{ background: white; border: 1px solid #dce3e6; border-radius: 12px; padding: 20px; margin-bottom: 16px; }}
    header {{ border-top: 5px solid #b45309; }}
    .badge {{ display: inline-block; background: #fff3cd; color: #7c2d12; border: 1px solid #f2c078; border-radius: 999px; padding: 5px 11px; font-weight: 800; letter-spacing: .04em; }}
    .warning {{ color: #713f12; background: #fffbeb; padding: 12px; border-radius: 8px; }}
    nav {{ display: flex; gap: 16px; flex-wrap: wrap; margin-top: 14px; }}
    a {{ color: #075985; }}
    table {{ width: 100%; border-collapse: collapse; font-size: .92rem; }}
    th, td {{ border-bottom: 1px solid #e5e7eb; text-align: left; padding: 9px; white-space: nowrap; }}
    .table-wrap {{ overflow-x: auto; }}
    svg {{ display: block; width: 100%; max-height: 360px; }}
    .axis {{ fill: #64748b; font-size: 11px; }}
    .small {{ color: #475569; font-size: .9rem; overflow-wrap: anywhere; }}
    code {{ overflow-wrap: anywhere; }}
    @media (max-width: 640px) {{ body {{ padding: 12px; }} header, section {{ padding: 14px; }} }}
  </style>
</head>
<body>
  <header id="resumo">
    <span class="badge">DADOS SINTÉTICOS · DEMO</span>
    <h1>QuantLab — relatório offline</h1>
    <p class="warning">{html.escape(SYNTHETIC_NOTICE)} Não use estes resultados como recomendação ou promessa de lucro.</p>
    <p><strong>RUN_ID:</strong> <code>{html.escape(run_id)}</code></p>
    <p><strong>Origem:</strong> fixture pública SINTÉTICA <code>{SYNTHETIC_DATASET_ID}</code> · {len(candles)} candles · {html.escape(str(candles[0].open_at_utc))} a {html.escape(str(candles[-1].end_at_utc))}</p>
    <p><strong>Estratégias:</strong> {html.escape(strategies)} · <strong>Evidence:</strong> DEMO</p>
    <p class="small">Criado em {_iso(created_at)} · SHA-256 da fixture <code>{fixture_hash}</code></p>
    <nav aria-label="Navegação do relatório">
      <a href="#resumo">Resumo</a><a href="#equity">Equity</a><a href="#fills">Fills</a><a href="#arquivos">Arquivos</a>
    </nav>
  </header>

  <section aria-labelledby="metrics-title">
    <h2 id="metrics-title">Resumo do run</h2>
    <div class="table-wrap"><table>
      <thead><tr><th>Estratégia</th><th>Capital inicial</th><th>Capital final</th><th>P&amp;L líquido</th><th>Retorno</th><th>Fees</th><th>Caixa final</th><th>Base final</th></tr></thead>
      <tbody>{''.join(summary_rows)}</tbody>
    </table></div>
  </section>

  <section id="equity">
    <h2>Equity marcada no fechamento de cada candle</h2>
    <p class="small">USDT = caixa + quantidade base × preço de fechamento. Linhas: CASH e BUY_HOLD. Custos da demo: fee de 0,10% e proxy de slippage de 5 bps por lado.</p>
    {_chart_svg(results)}
  </section>

  <section id="fills">
    <h2>Fills simulados</h2>
    <p class="small">O fill do BUY_HOLD usa o open elegível da primeira barra, com proxy de slippage explícita. A fixture contábil fixa de compra/venda é separada desta regra de sizing.</p>
    <div class="table-wrap"><table>
      <thead><tr><th>Estratégia</th><th>Timestamp UTC</th><th>Lado</th><th>Base qty</th><th>Preço executado</th><th>Fee</th><th>Caixa após fill</th><th>Base após fill</th></tr></thead>
      <tbody>{''.join(fill_rows)}</tbody>
    </table></div>
  </section>

  <section id="arquivos">
    <h2>Arquivos locais</h2>
    <ul><li><a href="run.json">run.json</a></li><li><a href="fills.csv">fills.csv</a></li><li><a href="equity.csv">equity.csv</a></li><li><a href="metrics.json">metrics.json</a></li></ul>
    <p class="small">Este HTML, seu gráfico SVG e os arquivos acima são locais; abrir ou reabrir o relatório não exige servidor, JavaScript externo ou rede.</p>
  </section>
</body>
</html>
"""


def write_demo_artifacts(
    output_root: Path,
    candles: list[Candle],
    results: list[BacktestResult],
) -> RunArtifacts:
    """Escreve um run completo em diretório exclusivo e o publica por rename."""
    if not candles or not results:
        raise ValueError("run precisa de candles e resultados")
    created_at = datetime.now(UTC)
    run_id = f"{created_at:%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    final_dir = output_root / run_id
    staging_dir = output_root / f".{run_id}.tmp"
    staging_dir.mkdir()

    fixture_hash = _fixture_sha256(candles)
    slippage_bps = "5"
    fee_rate = "0.001"
    run_manifest = {
        "artifact_schema_version": 1,
        "run_id": run_id,
        "created_at_utc": _iso(created_at),
        "phase": "F0",
        "evidence_level": "DEMO",
        "data_origin": "SYNTHETIC",
        "data_notice": SYNTHETIC_NOTICE,
        "git_sha": _git_revision(),
        "uv_lock_sha256": _lock_sha256(),
        "runtime": {"python": sys.version.split()[0]},
        "dataset": {
            "dataset_id": SYNTHETIC_DATASET_ID,
            "instrument_id": candles[0].instrument_id,
            "candle_count": len(candles),
            "interval": "1m",
            "start_open_at_utc": _iso(candles[0].open_at_utc),
            "end_at_utc": _iso(candles[-1].end_at_utc),
            "content_sha256": fixture_hash,
        },
        "strategies": [result.strategy.value for result in results],
        "execution_policy": {
            "market": "SPOT",
            "position_mode": "LONG_ONLY",
            "initial_cash_usdt": "1000.00",
            "assumed_data_delay_seconds": 1,
            "fill_on": "first_candle_open_strictly_after_available_at",
            "fill_assumption": "PROXY_OHLCV",
            "fee_rate_per_side": fee_rate,
            "fee_currency": "USDT",
            "slippage_proxy_bps_per_side": slippage_bps,
            "allow_short": False,
            "allow_leverage": False,
            "simulated_base_qty_step": _decimal_text(Decimal("0.00000001")),
            "end_position": "MARKED_NOT_FORCE_SOLD",
        },
        "file_formats": {
            "fills": "CSV",
            "equity": "CSV",
            "reason": "F0 usa CSV legível e sem dependência de Parquet.",
        },
    }
    metrics = {
        "evidence_level": "DEMO",
        "data_origin": "SYNTHETIC",
        "strategies": {result.strategy.value: _metrics_json(result) for result in results},
    }

    try:
        _write_json(staging_dir / "run.json", run_manifest)
        _write_fills(staging_dir / "fills.csv", results)
        _write_equity(staging_dir / "equity.csv", results)
        _write_json(staging_dir / "metrics.json", metrics)
        report = _render_report(run_id, created_at, candles, results, fixture_hash)
        (staging_dir / "report.html").write_text(report, encoding="utf-8")
        staging_dir.rename(final_dir)
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    return RunArtifacts(
        run_id=run_id,
        directory=final_dir,
        run_json=final_dir / "run.json",
        fills_csv=final_dir / "fills.csv",
        equity_csv=final_dir / "equity.csv",
        metrics_json=final_dir / "metrics.json",
        report_html=final_dir / "report.html",
    )

