"""F8: isolated public-feed Spot paper-forward session (no broker/orders)."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from quantlab.domain import PortfolioState
from quantlab.engine import SIMULATED_BASE_QTY_STEP
from quantlab.ledger import execute_market_target, mark_equity

VENUE = "BINANCE"
SYMBOL = "BTCUSDT"
INSTRUMENT_ID = "BINANCE:SPOT:BTCUSDT"
STREAM_URL = "wss://stream.binance.com:9443/stream?streams=btcusdt@kline_1m/btcusdt@aggTrade"
TIME_URL = "https://data-api.binance.vision/api/v3/time"
MAX_CLOCK_RTT_SECONDS = 2.0
MAX_SOURCE_LAG_SECONDS = 30.0
MAX_FUTURE_SKEW_SECONDS = 2.0
HEARTBEAT_SECONDS = 5.0
STALE_SECONDS = 45.0


class PaperForwardError(ValueError):
    """Invalid feed, timing, or ledger input; the session fails closed."""


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _decimal(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as error:
        raise PaperForwardError(f"{name} inválido") from error
    if not result.is_finite():
        raise PaperForwardError(f"{name} não é finito")
    return result


def sync_exchange_clock() -> tuple[datetime, float, float]:
    """Return server-aligned UTC anchor, monotonic anchor, and request RTT."""
    before_mono = time.monotonic()
    before_utc = datetime.now(UTC)
    request = urllib.request.Request(TIME_URL, headers={"User-Agent": "QuantLab-PAPER_FORWARD/1"})
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status != 200:
            raise PaperForwardError(f"relógio Binance HTTP {response.status}")
        endpoint = urlparse(response.geturl())
        if endpoint.scheme != "https" or endpoint.hostname != "data-api.binance.vision" or endpoint.port is not None:
            raise PaperForwardError("relógio redirecionado para host fora da allowlist")
        payload = json.loads(response.read(4096))
    after_mono = time.monotonic()
    after_utc = datetime.now(UTC)
    rtt = after_mono - before_mono
    if rtt > MAX_CLOCK_RTT_SECONDS:
        raise PaperForwardError(f"relógio incerto: RTT {rtt:.3f}s > {MAX_CLOCK_RTT_SECONDS:.1f}s")
    try:
        server_utc = datetime.fromtimestamp(int(payload["serverTime"]) / 1000, tz=UTC)
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise PaperForwardError("resposta de relógio inválida") from error
    local_midpoint = before_utc + (after_utc - before_utc) / 2
    server_anchor = local_midpoint + (server_utc - local_midpoint)
    mono_anchor = (before_mono + after_mono) / 2
    return server_anchor, mono_anchor, rtt


@dataclass(frozen=True, slots=True)
class ForwardConfig:
    initial_cash_usdt: Decimal = Decimal("1000")
    fee_bps_per_side: Decimal = Decimal("10")
    slippage_bps_per_side: Decimal = Decimal("5")
    max_drawdown_pct: Decimal = Decimal("30")
    signal_delay_ms: int = 1000

    def __post_init__(self) -> None:
        for name in ("initial_cash_usdt", "fee_bps_per_side", "slippage_bps_per_side", "max_drawdown_pct"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{name} deve ser Decimal finito")
        if self.initial_cash_usdt <= 0:
            raise ValueError("caixa inicial deve ser positivo")
        if not Decimal("0") <= self.fee_bps_per_side < Decimal("10000"):
            raise ValueError("fee bps deve estar entre 0 e 10000")
        if not Decimal("0") <= self.slippage_bps_per_side < Decimal("10000"):
            raise ValueError("slippage bps deve estar entre 0 e 10000")
        if not Decimal("0") < self.max_drawdown_pct <= Decimal("100"):
            raise ValueError("drawdown deve estar entre 0 e 100")
        if not 1 <= self.signal_delay_ms <= 60_000:
            raise ValueError("signal delay deve estar entre 1 e 60000 ms")


class ForwardEngine:
    """Only closed, post-activation candles can signal."""

    def __init__(
        self,
        *,
        config: ForwardConfig,
        root: Path,
        run_id: str,
        clock_anchor: datetime,
        monotonic_anchor: float,
        clock_rtt_seconds: float,
        active_at: datetime,
    ) -> None:
        if not run_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in run_id):
            raise ValueError("run_id inválido")
        self.config = config
        self.run_id = run_id
        self.path = root / "artifacts" / "paper_forward" / run_id
        self.path.mkdir(parents=True, exist_ok=False)
        self.clock_anchor = clock_anchor.astimezone(UTC)
        self.monotonic_anchor = monotonic_anchor
        self.clock_rtt_seconds = clock_rtt_seconds
        self.active_at = active_at.astimezone(UTC)
        self.first_eligible_open = self.active_at.replace(second=0, microsecond=0) + timedelta(minutes=1)
        self.state = PortfolioState(config.initial_cash_usdt, Decimal("0"))
        self.equity = config.initial_cash_usdt
        self.high_water = config.initial_cash_usdt
        self.max_drawdown = Decimal("0")
        self.status = "RUNNING"
        self.reason: str | None = None
        self.pending_signal: dict[str, Any] | None = None
        self.signal_created = False
        self.last_closed_open: datetime | None = None
        self.last_agg_id: int | None = None
        self.last_price: Decimal | None = None
        self.last_activity_mono = monotonic_anchor
        self.last_heartbeat_mono = monotonic_anchor
        self.journal_hash = "0" * 64
        self.journal_count = 0
        self._atomic_json(self.path / "run.json", {
            "run_id": run_id, "status": self.status, "evidence_level": "PAPER_FORWARD",
            "venue": VENUE, "symbol": SYMBOL, "instrument_id": INSTRUMENT_ID,
            "source": "Binance public production Spot WebSocket streams; no archive fallback",
            "stream_url": STREAM_URL, "clock_url": TIME_URL,
            "clock_rtt_seconds": clock_rtt_seconds,
            "clock_uncertainty_upper_bound_seconds": clock_rtt_seconds / 2,
            "activated_at_utc": _iso(active_at),
            "strategy": "BUY_HOLD on first fully observed closed 1m candle",
            "signal_delay_ms": config.signal_delay_ms,
            "initial_cash_usdt": str(config.initial_cash_usdt),
            "fee_bps_per_side_hypothesis": str(config.fee_bps_per_side),
            "slippage_bps_per_side_hypothesis": str(config.slippage_bps_per_side),
            "max_drawdown_pct": str(config.max_drawdown_pct),
            "license_status": "UNVERIFIED for retention/redistribution; see RFC",
            "real_orders": False,
        })
        self._write_state()

    def activate(self, mono_now: float) -> None:
        """Start the observation window only after the WebSocket subscription is open."""
        self.active_at = self.now_utc(mono_now)
        self.first_eligible_open = self.active_at.replace(second=0, microsecond=0) + timedelta(minutes=1)
        self.last_activity_mono = mono_now
        self.last_heartbeat_mono = mono_now
        manifest_path = self.path / "run.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["activated_at_utc"] = _iso(self.active_at)
        self._atomic_json(manifest_path, manifest)

    def now_utc(self, mono_now: float) -> datetime:
        return self.clock_anchor + timedelta(seconds=mono_now - self.monotonic_anchor)

    def _atomic_json(self, path: Path, payload: dict[str, Any]) -> None:
        temp = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        try:
            for attempt in range(6):
                try:
                    os.replace(temp, path)
                    return
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(0.05 * (2 ** attempt))
        finally:
            temp.unlink(missing_ok=True)

    def _write_state(self) -> None:
        self._atomic_json(self.path / "state.json", {
            "run_id": self.run_id, "status": self.status, "reason": self.reason,
            "cash_usdt": str(self.state.cash_usdt), "base_qty": str(self.state.base_qty),
            "fees_paid_usdt": str(self.state.fees_paid_usdt), "equity_usdt": str(self.equity),
            "high_water_usdt": str(self.high_water), "max_drawdown_pct": str(self.max_drawdown),
            "pending_available_at_utc": self.pending_signal["available_at_utc"] if self.pending_signal else None,
            "journal_records": self.journal_count, "journal_sha256": self.journal_hash,
        })

    def _record(self, kind: str, at: datetime, **fields: Any) -> None:
        payload = {
            "sequence": self.journal_count + 1, "kind": kind, "occurred_at_utc": _iso(at),
            "venue": VENUE, "instrument_id": INSTRUMENT_ID, "previous_sha256": self.journal_hash,
            **fields,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        current_hash = hashlib.sha256(encoded).hexdigest()
        with (self.path / "journal.jsonl").open("a", encoding="utf-8", newline="\n") as journal:
            journal.write(json.dumps({**payload, "sha256": current_hash}, ensure_ascii=False, sort_keys=True) + "\n")
            journal.flush()
        self.journal_hash = current_hash
        self.journal_count += 1
        self._write_state()

    def _halt(self, at: datetime, reason: str, **fields: Any) -> None:
        if self.status != "RUNNING":
            return
        self.status = "HALTED"
        self.reason = reason
        self._record("HALTED", at, reason=reason, **fields)

    def _source_time(self, value: Any, name: str) -> datetime:
        try:
            return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
        except (TypeError, ValueError, OverflowError, OSError) as error:
            raise PaperForwardError(f"timestamp {name} inválido") from error

    def _check_source_clock(self, event_time: datetime, receive_time: datetime) -> None:
        if event_time > receive_time + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS):
            raise PaperForwardError("FUTURE_SOURCE_TIMESTAMP")
        if (receive_time - event_time).total_seconds() > MAX_SOURCE_LAG_SECONDS:
            raise PaperForwardError("STALE_SOURCE_TIMESTAMP")

    def observe(self, message: dict[str, Any], *, receive_time: datetime, monotonic_now: float) -> None:
        if self.status != "RUNNING":
            return
        try:
            stream, data = message["stream"], message["data"]
            if not isinstance(stream, str) or not isinstance(data, dict):
                raise PaperForwardError("INVALID_ENVELOPE")
            event_time = self._source_time(data["E"], "E")
            self._check_source_clock(event_time, receive_time)
            self.last_activity_mono = monotonic_now
            if stream == "btcusdt@kline_1m" and data.get("e") == "kline":
                self._observe_kline(data, event_time, receive_time)
            elif stream == "btcusdt@aggTrade" and data.get("e") == "aggTrade":
                self._observe_trade(data, event_time, receive_time)
            else:
                raise PaperForwardError("UNEXPECTED_STREAM_OR_EVENT")
        except (KeyError, TypeError, ValueError, ArithmeticError, PaperForwardError) as error:
            self._halt(receive_time, str(error) or "INVALID_MARKET_EVENT")

    def _observe_kline(self, data: dict[str, Any], event_time: datetime, receive_time: datetime) -> None:
        kline = data.get("k")
        if data.get("s") != SYMBOL or not isinstance(kline, dict) or kline.get("i") != "1m":
            raise PaperForwardError("INSTRUMENT_OR_INTERVAL_MISMATCH")
        if not bool(kline.get("x")):
            return
        opened = self._source_time(kline["t"], "k.t")
        expected_close_ms = int(opened.timestamp() * 1000) + 60_000 - 1
        if int(kline["T"]) != expected_close_ms:
            raise PaperForwardError("INVALID_CANDLE_INTERVAL")
        if opened < self.first_eligible_open:
            self._record("PRE_ACTIVATION_CANDLE_IGNORED", receive_time, candle_open_at_utc=_iso(opened))
            return
        ended = opened + timedelta(minutes=1)
        if event_time < ended or receive_time < ended:
            raise PaperForwardError("CLOSED_CANDLE_BEFORE_INTERVAL_END")
        if self.last_closed_open is None and opened != self.first_eligible_open:
            raise PaperForwardError("FIRST_POST_ACTIVATION_CANDLE_MISSING")
        if self.last_closed_open is not None and opened != self.last_closed_open + timedelta(minutes=1):
            raise PaperForwardError("CLOSED_CANDLE_GAP_OR_REORDER")
        values = {key: _decimal(kline[field], field) for key, field in (
            ("open", "o"), ("high", "h"), ("low", "l"), ("close", "c"), ("volume", "v"),
        )}
        if min(values["open"], values["high"], values["low"], values["close"]) <= 0 or values["volume"] < 0:
            raise PaperForwardError("INVALID_CANDLE_VALUES")
        if values["high"] < max(values["open"], values["close"], values["low"]):
            raise PaperForwardError("INVALID_CANDLE_HIGH")
        if values["low"] > min(values["open"], values["close"], values["high"]):
            raise PaperForwardError("INVALID_CANDLE_LOW")
        self.last_closed_open = opened
        self._record("CLOSED_CANDLE", receive_time, candle_open_at_utc=_iso(opened),
                     candle_end_at_utc=_iso(ended), exchange_event_at_utc=_iso(event_time),
                     close_usdt=str(values["close"]))
        if not self.signal_created:
            available = max(receive_time, event_time) + timedelta(milliseconds=self.config.signal_delay_ms)
            self.pending_signal = {
                "available_at_utc": _iso(available), "available_at": available,
                "candle_end_at_utc": _iso(ended), "target_fraction": Decimal("1"),
            }
            self.signal_created = True
            self._record("SIGNAL", receive_time, strategy="BUY_HOLD", target_fraction="1",
                         generated_from_closed_candle_end_at_utc=_iso(ended),
                         available_at_utc=_iso(available), delay_ms=self.config.signal_delay_ms)

    def _observe_trade(self, data: dict[str, Any], event_time: datetime, receive_time: datetime) -> None:
        if data.get("s") != SYMBOL:
            raise PaperForwardError("INSTRUMENT_MISMATCH")
        try:
            aggregate_id = int(data["a"])
        except (KeyError, TypeError, ValueError) as error:
            raise PaperForwardError("INVALID_AGG_TRADE_ID") from error
        if aggregate_id < 0 or (self.last_agg_id is not None and aggregate_id != self.last_agg_id + 1):
            raise PaperForwardError("AGG_TRADE_SEQUENCE_GAP_OR_DUPLICATE")
        price = _decimal(data["p"], "trade price")
        if price <= 0:
            raise PaperForwardError("INVALID_TRADE_PRICE")
        trade_time = self._source_time(data["T"], "aggTrade.T")
        self._check_source_clock(trade_time, receive_time)
        self.last_agg_id = aggregate_id
        self.last_price = price
        equity_at_trade = mark_equity(self.state, price)
        self.high_water = max(self.high_water, equity_at_trade)
        drawdown = (self.high_water - equity_at_trade) / self.high_water * Decimal("100") if self.high_water else Decimal("0")
        self.max_drawdown = max(self.max_drawdown, drawdown)
        self.equity = equity_at_trade
        if drawdown >= self.config.max_drawdown_pct:
            self._halt(receive_time, "MAX_SIMULATION_DRAWDOWN_REACHED", drawdown_pct=str(drawdown),
                       aggregate_trade_id=aggregate_id, source_trade_at_utc=_iso(trade_time),
                       price_usdt=str(price))
            return
        if not self.pending_signal:
            return
        available = self.pending_signal["available_at"]
        # Both exchange time and server-aligned receipt must be strictly later.
        if trade_time <= available or receive_time <= available:
            return
        next_state, fill, result = execute_market_target(
            self.state, timestamp_utc=receive_time,
            target_fraction=self.pending_signal["target_fraction"], reference_open_usdt=price,
            fee_rate=self.config.fee_bps_per_side / Decimal("10000"),
            slippage_rate=self.config.slippage_bps_per_side / Decimal("10000"),
            quantity_step=SIMULATED_BASE_QTY_STEP,
        )
        if result != "FILLED" or fill is None or next_state.cash_usdt < 0 or next_state.base_qty <= 0:
            raise PaperForwardError(f"F3_LEDGER_BLOCKED:{result}")
        projected_equity = mark_equity(next_state, price)
        projected_high_water = max(self.high_water, projected_equity)
        projected_drawdown = (
            (projected_high_water - projected_equity) / projected_high_water * Decimal("100")
            if projected_high_water else Decimal("0")
        )
        if projected_drawdown >= self.config.max_drawdown_pct:
            self._halt(receive_time, "MAX_SIMULATION_DRAWDOWN_REACHED",
                       drawdown_pct=str(projected_drawdown),
                       limit_pct=str(self.config.max_drawdown_pct), fill_prevented=True)
            return
        self.state = next_state
        self.pending_signal = None
        self.equity = mark_equity(self.state, price)
        self.high_water = max(self.high_water, self.equity)
        post_fill_dd = (self.high_water - self.equity) / self.high_water * Decimal("100") if self.high_water else Decimal("0")
        self.max_drawdown = max(self.max_drawdown, post_fill_dd)
        self._record("SIMULATED_FILL", receive_time, fill_at_utc=_iso(fill.timestamp_utc),
                     source_trade_at_utc=_iso(trade_time), target_fraction="1", side=fill.side.value,
                     quantity_base=str(fill.quantity_base), execution_price_usdt=str(fill.price_usdt),
                     reference_price_usdt=str(fill.reference_price_usdt), quote_amount_usdt=str(fill.quote_amount_usdt),
                     fee_usdt=str(fill.fee_usdt), slippage_cost_usdt=str(fill.slippage_cost_usdt),
                     cash_after_usdt=str(fill.cash_after_usdt), base_qty_after=str(fill.base_qty_after),
                     equity_after_usdt=str(self.equity))

    def heartbeat(self, mono_now: float) -> None:
        if self.status != "RUNNING":
            return
        if mono_now - self.last_activity_mono > STALE_SECONDS:
            self._halt(self.now_utc(mono_now), "MARKET_FEED_STALE",
                       stale_seconds=mono_now - self.last_activity_mono)
            return
        if mono_now - self.last_heartbeat_mono >= HEARTBEAT_SECONDS:
            now = self.now_utc(mono_now)
            self._record("HEARTBEAT", now, feed_stale_after_seconds=STALE_SECONDS,
                         last_price_usdt=str(self.last_price) if self.last_price else None,
                         last_aggregate_trade_id=self.last_agg_id,
                         equity_usdt=str(self.equity), drawdown_pct=str(self.max_drawdown))
            self.last_heartbeat_mono = mono_now

    def finalize(self, status: str, reason: str, at: datetime) -> None:
        if self.status == "RUNNING":
            self.status, self.reason = status, reason
            self._record("SESSION_ENDED", at, status=status, reason=reason)
        manifest_path = self.path / "run.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update({"status": self.status, "reason": self.reason, "ended_at_utc": _iso(at)})
        self._atomic_json(manifest_path, manifest)
        self._write_state()
        fills = 1 if self.state.base_qty > 0 else 0
        report = """<!doctype html><meta charset="utf-8"><title>Paper Forward</title>
<h1>PAPER_FORWARD · BTCUSDT · {status}</h1><p>Feed público recente Binance Spot; simulação sem endpoint privado ou ordem real.</p>
<dl><dt>Instrumento</dt><dd>{instrument}</dd><dt>Estratégia</dt><dd>BUY_HOLD na primeira vela 1m fechada e totalmente observada após ativação</dd>
<dt>Fills simulados</dt><dd>{fills}</dd><dt>Cash</dt><dd>{cash} USDT</dd><dt>Quantidade</dt><dd>{qty} BTC</dd>
<dt>Equity marcada</dt><dd>{equity} USDT</dd><dt>Fees</dt><dd>{fees} USDT</dd><dt>Drawdown máximo observado</dt><dd>{drawdown}%</dd>
<dt>Motivo</dt><dd>{reason}</dd></dl><p>Journal encadeado: journal.jsonl</p>""".format(
            status=html.escape(self.status), instrument=INSTRUMENT_ID, fills=fills,
            cash=self.state.cash_usdt, qty=self.state.base_qty, equity=self.equity,
            fees=self.state.fees_paid_usdt, drawdown=self.max_drawdown,
            reason=html.escape(self.reason or "—"),
        )
        (self.path / "report.html").write_text(report, encoding="utf-8")


async def run_forward_session(
    *, root: Path, config: ForwardConfig, duration_seconds: int = 0,
    run_id: str | None = None, stop_file: Path | None = None,
) -> Path:
    if duration_seconds < 0:
        raise ValueError("duration_seconds não pode ser negativo")
    import websockets

    clock_anchor, mono_anchor, clock_rtt = await asyncio.to_thread(sync_exchange_clock)
    active_at = clock_anchor + timedelta(seconds=time.monotonic() - mono_anchor)
    engine = ForwardEngine(
        config=config, root=root, run_id=run_id or f"PF-{uuid4().hex[:12]}",
        clock_anchor=clock_anchor, monotonic_anchor=mono_anchor,
        clock_rtt_seconds=clock_rtt, active_at=active_at,
    )
    session_started = time.monotonic()
    try:
        async with websockets.connect(
            STREAM_URL, open_timeout=10, close_timeout=3, ping_interval=20, ping_timeout=20,
            max_size=1_048_576, compression=None,
        ) as socket:
            session_started = time.monotonic()
            engine.activate(session_started)
            while engine.status == "RUNNING":
                now_mono = time.monotonic()
                if stop_file and stop_file.exists():
                    stop_file.unlink(missing_ok=True)
                    engine.finalize("STOPPED", "USER_STOP_REQUESTED", engine.now_utc(now_mono))
                    return engine.path
                if duration_seconds and now_mono - session_started >= duration_seconds:
                    engine.finalize("STOPPED", "DURATION_REACHED", engine.now_utc(now_mono))
                    return engine.path
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    engine.heartbeat(time.monotonic())
                    continue
                receive_mono = time.monotonic()
                receive_time = engine.now_utc(receive_mono)
                try:
                    message = json.loads(raw)
                except (json.JSONDecodeError, TypeError) as error:
                    engine._halt(receive_time, f"INVALID_JSON:{error}")
                    break
                if not isinstance(message, dict):
                    engine._halt(receive_time, "INVALID_ENVELOPE")
                    break
                engine.observe(message, receive_time=receive_time, monotonic_now=receive_mono)
                engine.heartbeat(receive_mono)
    except asyncio.CancelledError:
        engine.finalize("STOPPED", "USER_INTERRUPT", engine.now_utc(time.monotonic()))
    except Exception as error:
        engine._halt(engine.now_utc(time.monotonic()), f"FEED_OR_SESSION_FAILURE:{type(error).__name__}:{error}")
    finally:
        if engine.status == "RUNNING":
            engine.finalize("HALTED", "UNEXPECTED_SESSION_EXIT", engine.now_utc(time.monotonic()))
        elif not (engine.path / "report.html").exists():
            engine.finalize(engine.status, engine.reason or "SESSION_ENDED", engine.now_utc(time.monotonic()))
    return engine.path


def run_forward(**kwargs: Any) -> Path:
    return asyncio.run(run_forward_session(**kwargs))
