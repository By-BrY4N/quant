"""CLI do QuantLab F0."""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

from quantlab.artifacts import write_demo_artifacts
from quantlab.backtest import BacktestConfig, run_backtest
from quantlab.backtest_artifacts import write_backtest_artifacts
from quantlab.data_ingestion import (
    ALLOWED_INTERVAL,
    ALLOWED_SYMBOL,
    ALLOWED_SYMBOLS,
    DEFAULT_MAX_TRANSFER_BYTES,
    IngestionError,
    daily_range,
    fetch_month,
    import_archive,
    month_range,
    print_plan,
)
from quantlab.data_quality import (
    DEFAULT_DELAY_MS,
    audit_raw_objects,
    create_snapshot,
    load_backtest_snapshot,
)
from quantlab.domain import StrategyName
from quantlab.engine import run_strategy
from quantlab.fixtures import SYNTHETIC_DATASET_ID, SYNTHETIC_NOTICE, synthetic_demo_candles
from quantlab.ml import run_ml_experiment
from quantlab.research import (
    METRIC_DIRECTIONS,
    bind_holdout_snapshot,
    create_experiment,
    ensure_snapshot_available,
    freeze_experiment,
    list_experiments,
    open_holdout,
    record_trial,
)


def _demo() -> int:
    candles = synthetic_demo_candles()
    results = [
        run_strategy(candles, strategy, initial_cash_usdt=Decimal("1000.00"))
        for strategy in (StrategyName.CASH, StrategyName.BUY_HOLD)
    ]
    artifacts = write_demo_artifacts(Path("artifacts"), candles, results)
    print("QuantLab F0 — demo offline")
    print("ORIGEM: SINTÉTICA — não representa dados de exchange nem evidência de lucro.")
    print(f"DATASET_ID={SYNTHETIC_DATASET_ID} | CANDLES={len(candles)}")
    print(f"RUN_ID={artifacts.run_id}")
    print(f"RELATÓRIO={artifacts.report_html}")
    for result in results:
        metrics = result.metrics
        print(
            f"{result.strategy.value}: equity final {metrics.final_equity_usdt:.2f} USDT; "
            f"fees {metrics.total_fees_usdt:.2f} USDT; "
            f"posição final {metrics.final_base_qty} base"
        )
    print(SYNTHETIC_NOTICE)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quantlab",
        description="Laboratório local de pesquisa quantitativa — dados, backtests e pesquisa cronológica F5.",
    )
    parser.add_argument("--version", action="version", version="quantlab 0.1.0")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser(
        "demo",
        help="executa CASH e BUY_HOLD sobre candles SINTÉTICOS e salva um relatório offline",
    )
    demo.set_defaults(handler=lambda _args: _demo())

    data = commands.add_parser("data", help="planeja, baixa ou importa ZIP diário/mensal Binance validado")
    data_commands = data.add_subparsers(dest="data_command", required=True)

    plan = data_commands.add_parser("plan", help="mostra arquivos e URLs; não baixa")
    plan.add_argument("--symbol", choices=ALLOWED_SYMBOLS, default=ALLOWED_SYMBOL)
    plan.add_argument("--interval", choices=(ALLOWED_INTERVAL,), default=ALLOWED_INTERVAL)
    plan.add_argument("--granularity", choices=("monthly", "daily"), default="monthly")
    plan.add_argument("--start", required=True, help="AAAA-MM para monthly; AAAA-MM-DD para daily")
    plan.add_argument("--end", required=True, help="AAAA-MM para monthly; AAAA-MM-DD para daily")
    plan.add_argument("--max-transfer-mb", type=int, default=DEFAULT_MAX_TRANSFER_BYTES // (1024 * 1024), help="teto agregado planejado em MiB")
    plan.add_argument("--offline", action="store_true", help="indica somente o que já existe no catálogo local")
    plan.add_argument("--root", type=Path, default=Path("."), help="raiz local do QuantLab (padrão: diretório atual)")
    plan.set_defaults(
        handler=lambda args: _data_plan(
            args.start, args.end, root=args.root, symbol=args.symbol, interval=args.interval,
            offline=args.offline, granularity=args.granularity,
            max_transfer_bytes=args.max_transfer_mb * 1024 * 1024,
        )
    )

    fetch = data_commands.add_parser("fetch", help="baixa ZIP e CHECKSUM oficiais com limite de transferência explícito")
    fetch.add_argument("--symbol", choices=ALLOWED_SYMBOLS, default=ALLOWED_SYMBOL)
    fetch.add_argument("--interval", choices=(ALLOWED_INTERVAL,), default=ALLOWED_INTERVAL)
    fetch.add_argument("--granularity", choices=("monthly", "daily"), default="monthly")
    fetch.add_argument("--start", required=True, help="AAAA-MM para monthly; AAAA-MM-DD para daily")
    fetch.add_argument("--end", required=True, help="AAAA-MM para monthly; AAAA-MM-DD para daily")
    fetch.add_argument("--max-transfer-mb", type=int, default=DEFAULT_MAX_TRANSFER_BYTES // (1024 * 1024), help="teto agregado de ZIPs realmente transferidos; padrão 512 MiB")
    fetch.add_argument("--refresh", action="store_true", help="consulta a origem novamente para detectar revisão remota")
    fetch.add_argument("--root", type=Path, default=Path("."), help="raiz local do QuantLab (padrão: diretório atual)")
    fetch.set_defaults(handler=_data_fetch)

    import_command = data_commands.add_parser("import", help="valida e importa ZIP oficial já baixado")
    import_command.add_argument("--zip", required=True, type=Path, help="BTCUSDT-1m-AAAA-MM.zip")
    import_command.add_argument("--checksum", type=Path, help="arquivo .CHECKSUM publicado junto ao ZIP")
    import_command.add_argument("--root", type=Path, default=Path("."), help="raiz local do QuantLab (padrão: diretório atual)")
    import_command.set_defaults(handler=_data_import)

    audit = data_commands.add_parser(
        "audit",
        help="normaliza um raw F1 exato para Parquet, calcula QA e exibe relatórios JSON/HTML",
    )
    audit.add_argument("--symbol", choices=ALLOWED_SYMBOLS, default=ALLOWED_SYMBOL)
    audit.add_argument(
        "--raw-object-id",
        action="append",
        help="ID raw F1 explícito; pode repetir. Sem ele, audita os objetos do símbolo no catálogo.",
    )
    audit.add_argument("--delay-ms", type=int, default=DEFAULT_DELAY_MS, help="atraso positivo ASSUMED após o fim do candle")
    audit.add_argument("--root", type=Path, default=Path("."), help="raiz local do QuantLab (padrão: diretório atual)")
    audit.set_defaults(handler=_data_audit)

    snapshot = data_commands.add_parser(
        "snapshot",
        help="cria snapshot imutável de partition_id PASS fornecidos explicitamente",
    )
    snapshot.add_argument(
        "--partition-id",
        action="append",
        required=True,
        help="ID exibido por data audit; pode repetir para fixar várias partições",
    )
    snapshot.add_argument(
        "--data-origin",
        choices=("REAL", "SYNTHETIC"),
        help="obrigatório para dados importados offline; aquisições oficiais remotas são REAL por padrão",
    )
    snapshot.add_argument("--root", type=Path, default=Path("."), help="raiz local do QuantLab (padrão: diretório atual)")
    snapshot.set_defaults(handler=_data_snapshot)

    backtest = commands.add_parser(
        "backtest",
        help="executa um backtest spot sobre um snapshot F2 QA PASS explicitamente nomeado",
    )
    backtest.add_argument("--snapshot", required=True, help="SNAPSHOT_ID exato; nunca seleciona latest")
    backtest.add_argument(
        "--strategy",
        choices=("cash", "buy_hold", "sma_cross", "breakout", "momentum", "mean_reversion"),
        default=StrategyName.SMA_CROSS.value.lower(),
    )
    backtest.add_argument("--timeframe", choices=("15m", "1h", "4h", "1d"), default="1h")
    backtest.add_argument("--cash-usdt", type=Decimal, default=Decimal("1000.00"))
    backtest.add_argument("--fee-bps", type=Decimal, default=Decimal("10"), help="fee quote por lado (hipótese)")
    backtest.add_argument("--slippage-bps", type=Decimal, default=Decimal("5"), help="slippage adverso por lado (hipótese)")
    backtest.add_argument(
        "--delay-ms",
        type=int,
        help="substitui explicitamente o delay do snapshot; positivo e assumido, nunca latência medida",
    )
    backtest.add_argument("--sma-fast", type=int, default=20)
    backtest.add_argument("--sma-slow", type=int, default=50)
    backtest.add_argument("--breakout-lookback", type=int, default=20)
    backtest.add_argument("--momentum-lookback", type=int, default=24)
    backtest.add_argument("--mean-reversion-lookback", type=int, default=30)
    backtest.add_argument("--mean-reversion-entry-z", type=Decimal, default=Decimal("-2"))
    backtest.add_argument("--mean-reversion-exit-z", type=Decimal, default=Decimal("0"))
    backtest.add_argument("--root", type=Path, default=Path("."), help="raiz local do QuantLab")
    backtest.add_argument("--output", type=Path, help="pasta de saída; padrão <root>/artifacts/backtests")
    backtest.set_defaults(handler=_backtest)

    research = commands.add_parser("research", help="registra trials, folds temporais e avaliação final única")
    research_commands = research.add_subparsers(dest="research_command", required=True)
    research_create = research_commands.add_parser("create", help="pré-registra uma família/versão antes dos trials")
    research_create.add_argument("--family", required=True)
    research_create.add_argument("--version", type=int, required=True)
    research_create.add_argument("--development-snapshot", required=True)
    research_create.add_argument("--holdout-snapshot")
    research_create.add_argument("--holdout-start", required=True, help="timestamp ISO UTC inclusivo")
    research_create.add_argument("--holdout-end", required=True, help="timestamp ISO UTC exclusivo")
    research_create.add_argument(
        "--strategy",
        choices=("CASH", "BUY_HOLD", "SMA_CROSS", "BREAKOUT", "MOMENTUM", "MEAN_REVERSION"),
        default="SMA_CROSS",
    )
    research_create.add_argument("--timeframe", choices=("15m", "1h", "4h", "1d"), default="1h")
    research_create.add_argument("--cash-usdt", type=Decimal, default=Decimal("1000"))
    research_create.add_argument("--fee-bps", type=Decimal, default=Decimal("10"))
    research_create.add_argument("--slippage-bps", type=Decimal, default=Decimal("5"))
    research_create.add_argument("--delay-ms", type=int, default=1000)
    research_create.add_argument("--sma-fast", type=int, default=20)
    research_create.add_argument("--sma-slow", type=int, default=50)
    research_create.add_argument("--breakout-lookback", type=int, default=20)
    research_create.add_argument("--momentum-lookback", type=int, default=24)
    research_create.add_argument("--mean-reversion-lookback", type=int, default=30)
    research_create.add_argument("--mean-reversion-entry-z", type=Decimal, default=Decimal("-2"))
    research_create.add_argument("--mean-reversion-exit-z", type=Decimal, default=Decimal("0"))
    research_create.add_argument("--allow-sma-fast", type=int, action="append")
    research_create.add_argument("--allow-sma-slow", type=int, action="append")
    research_create.add_argument("--allow-breakout-lookback", type=int, action="append")
    research_create.add_argument("--allow-momentum-lookback", type=int, action="append")
    research_create.add_argument("--allow-mean-reversion-lookback", type=int, action="append")
    research_create.add_argument("--max-attempts", type=int, default=5)
    research_create.add_argument("--train-bars", type=int, default=48)
    research_create.add_argument("--test-bars", type=int, default=12)
    research_create.add_argument("--max-folds", type=int, default=3)
    research_create.add_argument("--minimum-test-folds", type=int, default=2)
    research_create.add_argument("--label-horizon", type=int, help="campo registrado para modelos futuros; não aplicado em F5")
    research_create.add_argument("--purge-bars", type=int, default=0, help="campo registrado para modelos futuros; não aplicado em F5")
    research_create.add_argument("--primary-metric", choices=tuple(METRIC_DIRECTIONS), default="max_drawdown_pct")
    research_create.add_argument("--primary-threshold", type=Decimal, required=True)
    research_create.add_argument("--root", type=Path, default=Path("."))
    research_create.set_defaults(handler=_research_create)

    research_trial = research_commands.add_parser("trial", help="executa um trial pré-registrado no snapshot de desenvolvimento")
    research_trial.add_argument("--experiment", required=True)
    research_trial.add_argument("--sma-fast", type=int)
    research_trial.add_argument("--sma-slow", type=int)
    research_trial.add_argument("--breakout-lookback", type=int)
    research_trial.add_argument("--momentum-lookback", type=int)
    research_trial.add_argument("--mean-reversion-lookback", type=int)
    research_trial.add_argument("--root", type=Path, default=Path("."))
    research_trial.set_defaults(handler=_research_trial)

    research_bind = research_commands.add_parser("bind-holdout", help="vincula snapshot ainda não usado, mantendo-o selado")
    research_bind.add_argument("--experiment", required=True)
    research_bind.add_argument("--snapshot", required=True)
    research_bind.add_argument("--root", type=Path, default=Path("."))
    research_bind.set_defaults(handler=lambda args: _research_bind(args))

    research_freeze = research_commands.add_parser("freeze", help="congela uma versão e escolhe um trial")
    research_freeze.add_argument("--experiment", required=True)
    research_freeze.add_argument("--trial", required=True)
    research_freeze.add_argument("--root", type=Path, default=Path("."))
    research_freeze.set_defaults(handler=_research_freeze)

    research_holdout = research_commands.add_parser("holdout", help="abre a avaliação formal única de uma versão congelada")
    research_holdout.add_argument("--experiment", required=True)
    research_holdout.add_argument("--root", type=Path, default=Path("."))
    research_holdout.set_defaults(handler=_research_holdout)

    research_list = research_commands.add_parser("list", help="lista contratos e estado dos experimentos locais")
    research_list.add_argument("--root", type=Path, default=Path("."))
    research_list.set_defaults(handler=_research_list)

    ml = commands.add_parser("ml", help="executa um experimento supervisionado F6 em snapshot não selado")
    ml_commands = ml.add_subparsers(dest="ml_command", required=True)
    ml_run = ml_commands.add_parser("run", help="treina no passado, calibra na validação e compara no motor spot F3")
    ml_run.add_argument("--snapshot", required=True, help="SNAPSHOT_ID QA PASS explícito; holdout selado é recusado")
    ml_run.add_argument("--train-start", required=True, help="timestamp ISO-8601 UTC inclusivo")
    ml_run.add_argument("--train-end", required=True, help="timestamp ISO-8601 UTC exclusivo")
    ml_run.add_argument("--validation-start", required=True, help="timestamp ISO-8601 UTC inclusivo")
    ml_run.add_argument("--validation-end", required=True, help="timestamp ISO-8601 UTC exclusivo")
    ml_run.add_argument("--timeframe", choices=("1h", "4h"), default="1h")
    ml_run.add_argument("--horizon", type=int, default=6, help="H barras completas futuras para o label")
    ml_run.add_argument("--cash-usdt", type=Decimal, default=Decimal("1000"))
    ml_run.add_argument("--fee-bps", type=Decimal, default=Decimal("10"), help="fee hipotética quote por lado")
    ml_run.add_argument("--slippage-bps", type=Decimal, default=Decimal("5"), help="slippage proxy por lado")
    ml_run.add_argument("--delay-ms", type=int, help="override explícito do atraso do snapshot; padrão herda o snapshot")
    ml_run.add_argument("--sma-fast", type=int, default=20)
    ml_run.add_argument("--sma-slow", type=int, default=50)
    ml_run.add_argument("--root", type=Path, default=Path("."))
    ml_run.set_defaults(handler=_ml_run)

    paper_forward = commands.add_parser(
        "paper-forward", help="observa feed público Spot recente e executa somente ledger paper local"
    )
    paper_forward_commands = paper_forward.add_subparsers(dest="paper_forward_command", required=True)
    paper_forward_run = paper_forward_commands.add_parser("run", help="inicia sessão PAPER_FORWARD BTCUSDT Spot")
    paper_forward_run.add_argument("--cash-usdt", type=Decimal, default=Decimal("1000"))
    paper_forward_run.add_argument("--fee-bps", type=Decimal, default=Decimal("10"), help="fee hipotética por lado")
    paper_forward_run.add_argument("--slippage-bps", type=Decimal, default=Decimal("5"), help="slippage adverso por lado")
    paper_forward_run.add_argument("--max-drawdown-pct", type=Decimal, default=Decimal("30"))
    paper_forward_run.add_argument("--signal-delay-ms", type=int, default=1000)
    paper_forward_run.add_argument("--duration-seconds", type=int, default=0, help="0 aguarda Ctrl+C ou pedido de parada")
    paper_forward_run.add_argument("--run-id", help="identificador único opcional para a sessão")
    paper_forward_run.add_argument("--root", type=Path, default=Path("."))
    paper_forward_run.set_defaults(handler=_paper_forward_run)
    return parser


def _data_plan(
    start: str,
    end: str,
    *,
    root: Path,
    symbol: str,
    interval: str,
    offline: bool,
    granularity: str = "monthly",
    max_transfer_bytes: int = DEFAULT_MAX_TRANSFER_BYTES,
) -> int:
    print_plan(
        start, end, root=root, symbol=symbol, interval=interval, offline=offline,
        granularity=granularity, max_transfer_bytes=max_transfer_bytes,
    )
    return 0


def _data_fetch(args: argparse.Namespace) -> int:
    identities = month_range(args.start, args.end, symbol=args.symbol, interval=args.interval) if args.granularity == "monthly" else daily_range(args.start, args.end, symbol=args.symbol, interval=args.interval)
    if len(identities) > 31:
        raise ValueError("F5 limita cada comando a no máximo 31 ZIPs.")
    if args.max_transfer_mb <= 0:
        raise ValueError("--max-transfer-mb deve ser positivo")
    remaining = args.max_transfer_mb * 1024 * 1024
    failed = False
    for identity in identities:
        try:
            result = fetch_month(
                identity,
                root=args.root,
                refresh=args.refresh,
                max_transfer_bytes=remaining,
            )
            remaining -= result.transferred_bytes
            print(f"TRANSFERIDO {identity.key} bytes={result.transferred_bytes} status={result.status}")
        except (IngestionError, OSError) as error:
            consumed = int(getattr(error, "transferred_bytes", 0))
            remaining = max(remaining - consumed, 0)
            if consumed:
                print(f"TRANSFERIDO_ANTES_ERRO {identity.key} bytes={consumed}", file=sys.stderr)
            failed = True
            if isinstance(error, IngestionError) and error.code == "MISSING_NOT_CONFIRMED":
                print(f"MISSING_NOT_CONFIRMED {identity.key}: {error}", file=sys.stderr)
            else:
                print(f"ERRO {identity.key}: {error}", file=sys.stderr)
            break
    print(f"TRANSFERÊNCIA RESTANTE={remaining:,} bytes")
    return 1 if failed else 0


def _data_import(args: argparse.Namespace) -> int:
    result = import_archive(args.zip, args.checksum, root=args.root)
    if result.object_id is not None:
        print(f"RAW_OBJECT_ID={result.object_id}")
    if result.raw_path is not None:
        print(f"RAW={result.raw_path}")
    if result.manifest_path is not None:
        print(f"MANIFEST={result.manifest_path}")
    return 0


def _data_audit(args: argparse.Namespace) -> int:
    reports = audit_raw_objects(
        args.root,
        symbol=args.symbol,
        raw_object_ids=args.raw_object_id or (),
        delay_ms=args.delay_ms,
    )
    failed = False
    for report in reports:
        print(
            f"RAW_OBJECT_ID={report['raw_object_id']} QA={report['qa_status']} "
            f"UNIDADE={report.get('timestamp_unit', 'n/a')} DELAY_ASSUMED_MS={report['delay_assumed_ms']}"
        )
        print(
            f"RAW_SHA256={report['raw_sha256']} HEADER={report.get('header_present', 'n/a')} "
            f"LINHAS_FONTE={report.get('source_rows', 'n/a')} LINHAS_VALIDAS={report.get('valid_rows', 'n/a')} "
            f"LINHAS_INVALIDAS={report.get('invalid_rows_count', 'n/a')} DUPLICATAS_IDENTICAS_REMOVIDAS={report.get('identical_duplicate_rows', 'n/a')}"
        )
        print(
            f"PRIMEIRO_OPEN_UTC={report.get('first_open_at_utc', 'n/a')} "
            f"ULTIMO_OPEN_UTC={report.get('last_open_at_utc', 'n/a')}"
        )
        print("TIMEFRAME | QA | OBSERVADOS | ESPERADOS | LACUNAS/BLOQUEIOS | COBERTURA % | PARTITION_ID")
        for partition in report.get("partitions", []):
            print(
                f"{partition['timeframe']} | {partition['qa_status']} | {partition['row_count']} | "
                f"{partition['expected_count']} | {partition['gap_count']} | {partition['coverage_pct']} | "
                f"{partition.get('partition_id') or '-'}"
            )
        print(f"JANELAS_1H_INCOMPLETAS={report.get('aggregate_windows', {}).get('1h', {}).get('incomplete', 'n/a')}")
        print(f"JANELAS_4H_INCOMPLETAS={report.get('aggregate_windows', {}).get('4h', {}).get('incomplete', 'n/a')}")
        for gap in report.get("missing_1m_ranges", []):
            print(
                f"GAP_1M={gap['start_at_utc']}..{gap['end_at_utc_exclusive']} "
                f"MINUTOS_AUSENTES={gap['missing_minutes']}"
            )
        print(f"QA_JSON={report.get('qa_json_path', '')}")
        print(f"QA_HTML={report.get('qa_html_path', '')}")
        if report["qa_status"] != "PASS":
            failed = True
            print(f"QUARANTINE={report.get('quarantine_path', 'registrada no catálogo')}")
        elif report.get("incomplete_windows"):
            print("AVISO: janelas incompletas foram bloqueadas; sem ffill e sem agregação parcial.")
    return 1 if failed else 0


def _data_snapshot(args: argparse.Namespace) -> int:
    result = create_snapshot(args.root, args.partition_id, data_origin=args.data_origin)
    print(f"SNAPSHOT_ID={result['snapshot_id']} QA={result['qa_status']} ORIGEM={result['data_origin']}")
    print(f"CONTENT_SHA256={result['content_sha256']}")
    print("PARTITION_IDS=" + ",".join(result["partition_ids"]))
    print(f"MANIFEST={result['manifest_path']}")
    print(f"QA_JSON={result['qa_json_path']}")
    print(f"QA_HTML={result['qa_html_path']}")
    if result.get("status"):
        print(result["status"])
    return 0


def _backtest(args: argparse.Namespace) -> int:
    ensure_snapshot_available(args.root, args.snapshot)
    snapshot = load_backtest_snapshot(args.root, args.snapshot)
    source_delay_ms = int((snapshot.source_metadata or {}).get("delay_assumed_ms", DEFAULT_DELAY_MS))
    delay_ms = source_delay_ms if args.delay_ms is None else args.delay_ms
    if delay_ms <= 0:
        raise ValueError("--delay-ms deve ser positivo")
    config = BacktestConfig(
        strategy=StrategyName(args.strategy.upper()),
        decision_timeframe=args.timeframe,
        initial_cash_usdt=args.cash_usdt,
        fee_bps_per_side=args.fee_bps,
        slippage_bps_per_side=args.slippage_bps,
        assumed_data_delay_seconds=Decimal(delay_ms) / Decimal("1000"),
        sma_fast=args.sma_fast,
        sma_slow=args.sma_slow,
        breakout_lookback=args.breakout_lookback,
        momentum_lookback=args.momentum_lookback,
        mean_reversion_lookback=args.mean_reversion_lookback,
        mean_reversion_entry_z=args.mean_reversion_entry_z,
        mean_reversion_exit_z=args.mean_reversion_exit_z,
    )
    result = run_backtest(snapshot, config)
    if args.output is None:
        output_root = args.root / "artifacts" / "backtests"
    elif args.output.is_absolute():
        output_root = args.output
    else:
        output_root = args.root / args.output
    artifacts = write_backtest_artifacts(output_root, result)
    print("QuantLab F3 — backtest SPOT long-only; sem short, alavancagem, margem ou ordem real.")
    print(
        f"SNAPSHOT_ID={snapshot.snapshot_id} QA={snapshot.qa_status} ORIGEM={snapshot.data_origin} "
        f"EVIDÊNCIA={result.audit['evidence_level']}"
    )
    print(
        f"DELAY_SNAPSHOT_MS={source_delay_ms} DELAY_BACKTEST_MS={delay_ms} "
        "CONFIANÇA=ASSUMED (hipótese; não latência medida)"
    )
    print("FILL=primeiro open 1m estritamente posterior; preço=OPEN 1m + slippage proxy; fee quote separada.")
    print("FEE/SLIPPAGE são hipóteses configuradas, não taxas oficiais Binance; OHLCV não garante fill.")
    print(f"RUN_ID={artifacts['run_id']}")
    print(f"RUN_DIR={artifacts['directory']}")
    print(f"RELATÓRIO={artifacts['report_html']}")
    print(f"METRICS_JSON={artifacts['metrics_json']}")
    print(
        f"GAP_COUNT={result.audit['gap_count']} GAPS_1M_MINUTOS={result.audit['missing_1m_minutes']} "
        f"GAP_EXEMPLOS_OMITIDOS={result.audit['gap_examples_omitted']} "
        f"JANELAS_{args.timeframe}_INCOMPLETAS={result.audit['discarded_incomplete_decision_windows']}"
    )
    print("WARMUP_JANELAS_DESCARTADAS=" + str(result.audit["warmup_windows_discarded"]))
    for item in result.results:
        metrics = item.metrics
        win_rate = (
            f"{metrics.win_rate_pct}%"
            if metrics.win_rate_pct is not None
            else "N/D"
        )
        print(
            f"{item.strategy.value}: equity={metrics.final_equity_usdt} USDT; "
            f"P&L bruto={metrics.gross_pnl_usdt}; fees={metrics.total_fees_usdt}; "
            f"slippage={metrics.total_slippage_cost_usdt}; líquido={metrics.net_pnl_usdt}; "
            f"Sharpe_diário={metrics.daily_sharpe if metrics.daily_sharpe is not None else 'N/D'} "
            f"(n={metrics.daily_sharpe_observation_count}, anualização=sqrt({metrics.daily_sharpe_annualization_days}), "
            f"rf={metrics.daily_sharpe_risk_free_daily_return}); "
            f"win_rate={win_rate}; "
            f"profit_factor={metrics.profit_factor if metrics.profit_factor is not None else 'N/D'}; "
            f"trades_fechados={metrics.closed_trade_count}; "
            f"vetos={metrics.veto_count}; posição_aberta={metrics.open_position}; "
            f"caixa={metrics.final_cash_usdt}; base={metrics.final_base_qty}"
        )
    return 0


def _research_create(args: argparse.Namespace) -> int:
    config = BacktestConfig(
        strategy=StrategyName(args.strategy),
        decision_timeframe=args.timeframe,
        initial_cash_usdt=args.cash_usdt,
        fee_bps_per_side=args.fee_bps,
        slippage_bps_per_side=args.slippage_bps,
        assumed_data_delay_seconds=Decimal(args.delay_ms) / Decimal("1000"),
        sma_fast=args.sma_fast,
        sma_slow=args.sma_slow,
        breakout_lookback=args.breakout_lookback,
        momentum_lookback=args.momentum_lookback,
        mean_reversion_lookback=args.mean_reversion_lookback,
        mean_reversion_entry_z=args.mean_reversion_entry_z,
        mean_reversion_exit_z=args.mean_reversion_exit_z,
    )
    allowed: dict[str, list[int]] = {}
    if args.strategy == "SMA_CROSS":
        allowed = {
            "sma_fast": sorted(set((args.allow_sma_fast or []) + [args.sma_fast])),
            "sma_slow": sorted(set((args.allow_sma_slow or []) + [args.sma_slow])),
        }
    elif args.strategy == "BREAKOUT":
        allowed = {"breakout_lookback": sorted(set((args.allow_breakout_lookback or []) + [args.breakout_lookback]))}
    elif args.strategy == "MOMENTUM":
        allowed = {"momentum_lookback": sorted(set((args.allow_momentum_lookback or []) + [args.momentum_lookback]))}
    elif args.strategy == "MEAN_REVERSION":
        allowed = {"mean_reversion_lookback": sorted(set((args.allow_mean_reversion_lookback or []) + [args.mean_reversion_lookback]))}
    result = create_experiment(
        args.root,
        family_id=args.family,
        version=args.version,
        development_snapshot_id=args.development_snapshot,
        holdout_snapshot_id=args.holdout_snapshot,
        holdout_start_at_utc=args.holdout_start,
        holdout_end_at_utc=args.holdout_end,
        config=config,
        max_attempts=args.max_attempts,
        train_bars=args.train_bars,
        test_bars=args.test_bars,
        max_folds=args.max_folds,
        primary_metric=args.primary_metric,
        primary_direction=METRIC_DIRECTIONS[args.primary_metric],
        primary_threshold=args.primary_threshold,
        minimum_test_folds=args.minimum_test_folds,
        allowed_parameter_values=allowed,
        label_horizon=args.label_horizon,
        purge_bars=args.purge_bars,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _research_trial(args: argparse.Namespace) -> int:
    item = next((row for row in list_experiments(args.root) if row["experiment_id"] == args.experiment), None)
    if item is None:
        raise ValueError(f"Experimento não encontrado: {args.experiment}")
    base = item["spec"]["base_config"]
    config = BacktestConfig(
        strategy=StrategyName(str(base["strategy"]).upper()),
        decision_timeframe=str(base["decision_timeframe"]),
        initial_cash_usdt=Decimal(str(base["initial_cash_usdt"])),
        fee_bps_per_side=Decimal(str(base["fee_bps_per_side"])),
        slippage_bps_per_side=Decimal(str(base["slippage_bps_per_side"])),
        assumed_data_delay_seconds=Decimal(str(base["assumed_data_delay_seconds"])),
        sma_fast=base["sma_fast"] if args.sma_fast is None else args.sma_fast,
        sma_slow=base["sma_slow"] if args.sma_slow is None else args.sma_slow,
        breakout_lookback=base["breakout_lookback"] if args.breakout_lookback is None else args.breakout_lookback,
        momentum_lookback=base.get("momentum_lookback", 24) if args.momentum_lookback is None else args.momentum_lookback,
        mean_reversion_lookback=base.get("mean_reversion_lookback", 30) if args.mean_reversion_lookback is None else args.mean_reversion_lookback,
        mean_reversion_entry_z=Decimal(str(base.get("mean_reversion_entry_z", "-2"))),
        mean_reversion_exit_z=Decimal(str(base.get("mean_reversion_exit_z", "0"))),
    )
    result = record_trial(args.root, args.experiment, config)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _research_bind(args: argparse.Namespace) -> int:
    result = bind_holdout_snapshot(args.root, args.experiment, args.snapshot)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _research_freeze(args: argparse.Namespace) -> int:
    result = freeze_experiment(args.root, args.experiment, args.trial)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _research_holdout(args: argparse.Namespace) -> int:
    result = open_holdout(args.root, args.experiment)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _research_list(args: argparse.Namespace) -> int:
    print(json.dumps(list_experiments(args.root), indent=2, ensure_ascii=False))
    return 0


def _ml_run(args: argparse.Namespace) -> int:
    options = {
        "horizon": args.horizon,
        "train_start_at_utc": args.train_start,
        "train_end_at_utc_exclusive": args.train_end,
        "validation_start_at_utc": args.validation_start,
        "validation_end_at_utc_exclusive": args.validation_end,
        "timeframe": args.timeframe,
        "initial_cash_usdt": args.cash_usdt,
        "fee_bps_per_side": args.fee_bps,
        "slippage_bps_per_side": args.slippage_bps,
        "sma_fast": args.sma_fast,
        "sma_slow": args.sma_slow,
    }
    if args.delay_ms is not None:
        if args.delay_ms <= 0:
            raise ValueError("--delay-ms deve ser positivo")
        options["assumed_data_delay_seconds"] = Decimal(args.delay_ms) / Decimal("1000")
    result = run_ml_experiment(args.root, args.snapshot, **options)
    print(json.dumps({
        "experiment_id": result["manifest"]["experiment_id"],
        "incremental_status": result["manifest"]["incremental_status"],
        "evidence_level": result["manifest"]["evidence_level"],
        "training_examples": result["manifest"]["sample_counts"]["training_examples"],
        "validation_examples": result["manifest"]["sample_counts"]["validation_examples"],
        "trial_count": result["manifest"]["threshold_calibration"]["trial_count"],
        "backtest_run_id": result["paths"]["backtest_run_id"],
        "artifacts": {key: str(value) for key, value in result["paths"].items()},
    }, indent=2, ensure_ascii=False))
    return 0


def _paper_forward_run(args: argparse.Namespace) -> int:
    from quantlab.paper_forward import ForwardConfig, run_forward

    config = ForwardConfig(
        initial_cash_usdt=args.cash_usdt,
        fee_bps_per_side=args.fee_bps,
        slippage_bps_per_side=args.slippage_bps,
        max_drawdown_pct=args.max_drawdown_pct,
        signal_delay_ms=args.signal_delay_ms,
    )
    path = run_forward(
        root=args.root.resolve(), config=config, duration_seconds=args.duration_seconds,
        run_id=args.run_id, stop_file=args.root.resolve() / "artifacts" / "paper_forward" / "stop.request",
    )
    manifest = json.loads((path / "run.json").read_text(encoding="utf-8"))
    state = json.loads((path / "state.json").read_text(encoding="utf-8"))
    print(json.dumps({"run_id": manifest["run_id"], "status": manifest["status"],
                      "reason": manifest["reason"], "instrument_id": manifest["instrument_id"],
                      "evidence_level": manifest["evidence_level"], "fills": int(Decimal(state["base_qty"]) > 0),
                      "artifacts": {"run": str(path / "run.json"), "journal": str(path / "journal.jsonl"),
                                    "state": str(path / "state.json"), "report": str(path / "report.html")}},
                     indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (OSError, ValueError) as error:
        print(f"ERRO: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

