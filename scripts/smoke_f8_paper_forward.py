from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from quantlab.paper_forward import ForwardConfig, run_forward_session


def _verify_hash_chain(records: list[dict]) -> None:
    previous = "0" * 64
    for index, record in enumerate(records, start=1):
        if record["sequence"] != index or record["previous_sha256"] != previous:
            raise AssertionError("sequência/hash anterior do journal divergente")
        payload = {key: value for key, value in record.items() if key != "sha256"}
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if record["sha256"] != digest:
            raise AssertionError("hash do journal inválido")
        previous = digest


async def _run(root: Path, duration_seconds: int) -> Path:
    return await run_forward_session(
        root=root, config=ForwardConfig(), duration_seconds=duration_seconds,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke integrado F8 usando feed público Binance Spot recente.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--duration-seconds", type=int, default=150)
    args = parser.parse_args()
    if args.duration_seconds < 130:
        parser.error("o smoke requer pelo menos 130 segundos para observar uma vela de 1m inteira")
    path = asyncio.run(_run(args.root.resolve(), args.duration_seconds))
    manifest = json.loads((path / "run.json").read_text(encoding="utf-8"))
    state = json.loads((path / "state.json").read_text(encoding="utf-8"))
    records = [json.loads(line) for line in (path / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
    _verify_hash_chain(records)
    candles = [item for item in records if item["kind"] == "CLOSED_CANDLE"]
    signals = [item for item in records if item["kind"] == "SIGNAL"]
    fills = [item for item in records if item["kind"] == "SIMULATED_FILL"]
    if manifest["evidence_level"] != "PAPER_FORWARD" or manifest["real_orders"] is not False:
        raise AssertionError("nível de evidência ou proteção de ordens reais incorreto")
    if manifest["status"] != "STOPPED" or manifest["reason"] != "DURATION_REACHED":
        raise AssertionError(f"sessão não concluiu duração: {manifest['status']} / {manifest['reason']}")
    if not candles or not signals or len(fills) != 1:
        raise AssertionError("feed recente não produziu vela fechada, sinal e exatamente um fill paper")
    available = signals[0]["available_at_utc"]
    if fills[0]["source_trade_at_utc"] <= available or fills[0]["fill_at_utc"] <= available:
        raise AssertionError("fill paper não ocorreu estritamente após disponibilidade")
    if Decimal(state["cash_usdt"]) < 0 or Decimal(state["base_qty"]) <= 0:
        raise AssertionError("ledger paper ficou inválido")
    print(json.dumps({
        "result": "F8_PAPER_FORWARD_SMOKE_OK",
        "run_id": manifest["run_id"],
        "status": manifest["status"],
        "recent_feed": manifest["stream_url"],
        "closed_candles": len(candles),
        "simulated_fills": len(fills),
        "fee_usdt": fills[0]["fee_usdt"],
        "license_status": manifest["license_status"],
        "artifacts": {key: str(path / filename) for key, filename in {
            "run": "run.json", "journal": "journal.jsonl", "state": "state.json", "report": "report.html",
        }.items()},
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
