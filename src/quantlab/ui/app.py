"""Streamlit app for the local F4 QuantLab release."""

from __future__ import annotations

import csv
import io
import json
import logging
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pandas as pd
import plotly.express as px
import streamlit as st

from quantlab.artifacts import write_demo_artifacts
from quantlab.backtest import BacktestConfig, run_backtest
from quantlab.backtest_artifacts import write_backtest_artifacts
from quantlab.data_ingestion import (
    ALLOWED_SYMBOLS,
    DEFAULT_MAX_TRANSFER_BYTES,
    MAX_CHECKSUM_BYTES,
    MonthIdentity,
    archive_url,
    build_daily_identity,
    checksum_url,
    daily_range,
    fetch_month,
    import_archive,
    month_range,
)
from quantlab.data_quality import audit_raw_objects, create_snapshot, load_backtest_snapshot
from quantlab.domain import StrategyName
from quantlab.engine import run_strategy
from quantlab.fixtures import SYNTHETIC_NOTICE, synthetic_demo_candles
from quantlab.ml import list_ml_runs, run_ml_experiment
from quantlab.paper_replay import (
    ReplayError,
    ReplayStatus,
    RiskLimits,
    advance_paper_replay,
    compare_paper_replay_to_backtest,
    create_paper_replay,
    export_equity_csv,
    export_paper_replay_html,
    export_paper_replay_journal,
    get_paper_replay,
    list_paper_replay_events,
    list_paper_replays,
    pause_paper_replay,
    reconcile_paper_replay,
    reset_paper_replay,
    resume_paper_replay,
    stop_paper_replay,
)
from quantlab.research import (
    METRIC_DIRECTIONS,
    ResearchError,
    bind_holdout_snapshot,
    create_experiment,
    ensure_snapshot_available,
    freeze_experiment,
    list_experiments,
    list_research_events,
    load_trial_manifest,
    open_holdout,
    record_trial,
)
from quantlab.ui.storage import list_runs, load_catalog_overview, load_run


PAGE_NAMES = ("Dados", "Backtests", "Operações", "Comparar", "Pesquisa", "ML", "Paper Replay", "Paper Forward", "Runs anteriores")
STRATEGY_LABELS = {
    "CASH": "Cash",
    "BUY_HOLD": "Buy & hold",
    "SMA_CROSS": "SMA",
    "BREAKOUT": "Breakout",
    "MOMENTUM": "Momentum",
    "MEAN_REVERSION": "Mean reversion",
    "ML_NULL": "ML · modelo nulo",
    "ML_LOGISTIC_PRICE": "ML · logística preço",
    "ML_LOGISTIC_PRICE_VOLUME": "ML · logística preço + volume",
}
OPERATION_COLUMNS = (
    "strategy",
    "signal_at_utc",
    "available_at_utc",
    "fill_at_utc",
    "side",
    "reference_price_usdt",
    "execution_price_usdt",
    "quantity_base",
    "fee_usdt",
    "slippage_cost_usdt",
    "realized_pnl_usdt",
    "execution_status",
    "veto_reason",
    "reason",
)


def _project_root() -> Path:
    return Path.cwd().resolve()


def _log_error(root: Path, action: str, error: Exception) -> None:
    log_path = root / "state" / "streamlit.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger("quantlab.ui")
        if not any(getattr(handler, "baseFilename", None) == str(log_path) for handler in logger.handlers):
            handler = logging.FileHandler(log_path, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
        logger.exception("Ação da interface falhou: %s", action, exc_info=error)
    except OSError:
        logging.getLogger("quantlab.ui").exception("Ação da interface falhou: %s", action)


def _show_error(root: Path, action: str, error: Exception) -> None:
    _log_error(root, action, error)
    message = str(error).strip().replace("\n", " ") or error.__class__.__name__
    if len(message) > 280:
        message = message[:277] + "..."
    st.error(f"{action}: {message}")
    st.caption(f"Detalhe técnico e traceback local: {root / 'state' / 'streamlit.log'}")


def _safe_upload_name(name: str) -> str:
    leaf = name.replace("\\", "/").rsplit("/", 1)[-1]
    if not leaf or leaf in {".", ".."}:
        raise ValueError("O nome do arquivo enviado não é válido.")
    return leaf


def _default_closed_month() -> str:
    current = datetime.now(UTC)
    year, month = current.year, current.month - 1
    if month == 0:
        year, month = year - 1, 12
    return f"{year:04d}-{month:02d}"


def _capture_fetch(identity: MonthIdentity, root: Path, max_transfer_bytes: int = 64 * 1024 * 1024):
    output = io.StringIO()
    with redirect_stdout(output):
            result = fetch_month(identity, root=root, max_transfer_bytes=max_transfer_bytes)
    return result, output.getvalue()


def _run_demo(root: Path) -> str:
    candles = synthetic_demo_candles()
    results = [
        run_strategy(candles, strategy, initial_cash_usdt=Decimal("1000.00"))
        for strategy in (StrategyName.CASH, StrategyName.BUY_HOLD)
    ]
    return write_demo_artifacts(root / "artifacts", candles, results).run_id


def _data_status(catalog: dict, runs: list[dict]) -> tuple[str, str]:
    snapshots = catalog.get("snapshots", [])
    if any(item.get("data_origin") == "REAL" and item.get("qa_status") == "PASS" for item in snapshots):
        return "DADOS REAIS", "Há pelo menos um snapshot real QA PASS no catálogo local."
    if any(item.get("data_origin") == "SYNTHETIC" for item in snapshots) or any(
        item.get("data_origin") == "SYNTHETIC" for item in runs
    ):
        return "DEMO DISPONÍVEL", "A origem sintética está identificada; ela demonstra o fluxo, não uma estratégia."
    if catalog.get("raw_objects") or catalog.get("partitions"):
        return "DADOS IMPORTADOS · AUDITORIA PENDENTE", "Há objetos locais; confira checksum, QA e snapshot antes de rodar."
    return "VAZIO", "Nenhum dataset ou run local foi encontrado. A próxima ação é executar a demo ou importar um mês."


def _render_catalog_status(root: Path, catalog: dict, runs: list[dict]) -> None:
    label, description = _data_status(catalog, runs)
    st.subheader(label)
    st.write(description)
    cards = st.columns(4)
    cards[0].metric("Raw registrados", len(catalog.get("raw_objects", [])))
    cards[1].metric("Partições QA PASS", len(catalog.get("partitions", [])))
    cards[2].metric("Snapshots", len(catalog.get("snapshots", [])))
    cards[3].metric("Runs", len(runs))
    st.caption(f"Pasta local do projeto: {root}")


def _render_data_page(root: Path, catalog: dict, runs: list[dict]) -> None:
    st.header("Dados")
    st.write("Consulte a origem, audite ZIPs diários ou mensais e planeje backfills curtos para BTCUSDT ou ETHUSDT.")
    _render_catalog_status(root, catalog, runs)
    st.divider()

    st.subheader("Demo offline")
    st.info(f"{SYNTHETIC_NOTICE} Nenhum resultado da demo é evidência de vantagem de estratégia.")
    if st.button("Executar demo sintética", type="primary", key="run_demo"):
        try:
            run_id = _run_demo(root)
            st.session_state["selected_run_id"] = run_id
            st.success(f"Demo concluída. RUN_ID: {run_id}")
            st.rerun()
        except Exception as error:  # noqa: BLE001 - mensagem curta ao usuário; traceback fica no log local.
            _show_error(root, "Não foi possível executar a demo", error)

    st.divider()
    st.subheader("Importar ZIP Binance Spot 1m")
    st.write("Envie um ZIP Spot 1m com nome oficial de BTCUSDT ou ETHUSDT, diário (`SYMBOL-1m-AAAA-MM-DD.zip`) ou mensal (`SYMBOL-1m-AAAA-MM.zip`). O CHECKSUM é opcional; sem ele, a origem fica marcada como não verificada.")
    zip_upload = st.file_uploader("ZIP Spot 1m diário ou mensal", type=["zip"], max_upload_size=64, key="zip_upload")
    checksum_upload = st.file_uploader("Arquivo .CHECKSUM correspondente (opcional)", type=["CHECKSUM", "checksum", "txt"], max_upload_size=1, key="checksum_upload")
    if st.button("Importar e validar o ZIP enviado", disabled=zip_upload is None, key="import_zip"):
        try:
            with tempfile.TemporaryDirectory(prefix="quantlab-upload-") as temporary:
                zip_path = Path(temporary) / _safe_upload_name(zip_upload.name)
                zip_path.write_bytes(zip_upload.getvalue())
                checksum_path = None
                if checksum_upload is not None:
                    checksum_path = Path(temporary) / _safe_upload_name(checksum_upload.name)
                    checksum_path.write_bytes(checksum_upload.getvalue())
                result = import_archive(zip_path, checksum_path, root=root)
            if result.integrity_status == "VERIFIED":
                st.success(f"Importação concluída: {result.status} · objeto {result.object_id} · CHECKSUM verificado.")
            else:
                st.warning(f"Importação concluída: {result.status} · objeto {result.object_id} · integridade UNVERIFIED; não crie um snapshot real sem confirmar a origem.")
            st.rerun()
        except Exception as error:  # noqa: BLE001
            _show_error(root, "Falha na importação; o objeto rejeitado permanece em quarentena quando aplicável", error)

    st.divider()
    st.subheader("Planejar backfill — sem download até confirmar")
    with st.form("fetch_plan_form"):
        data_symbol = st.selectbox("Instrumento Spot", ALLOWED_SYMBOLS, key="data_plan_symbol")
        granularity = st.selectbox("Origem do arquivo", ["monthly", "daily"], format_func=lambda value: "Mensal" if value == "monthly" else "Diária", key="data_granularity")
        if granularity == "monthly":
            start_text = st.text_input("Primeiro mês fechado (AAAA-MM)", value=_default_closed_month(), key="plan_month_start")
            end_text = st.text_input("Último mês fechado (AAAA-MM)", value=_default_closed_month(), key="plan_month_end")
            max_zips_help = "Até 3 meses por comando."
        else:
            yesterday = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
            start_text = st.text_input("Primeiro dia UTC fechado (AAAA-MM-DD)", value=yesterday, key="plan_day_start")
            end_text = st.text_input("Último dia UTC fechado (AAAA-MM-DD)", value=yesterday, key="plan_day_end")
            max_zips_help = "Até 31 ZIPs diários por comando."
        max_transfer_mb = int(st.number_input(
            "Limite agregado de transferência (MiB)",
            min_value=1,
            max_value=2048,
            value=DEFAULT_MAX_TRANSFER_BYTES // (1024 * 1024),
            step=32,
            help="Teto aplicado ao total transferido; cada ZIP tem seu próprio limite de 64 MiB.",
        ))
        st.caption(max_zips_help)
        plan_requested = st.form_submit_button("Preparar plano")
    if plan_requested:
        try:
            identities = (
                month_range(start_text, end_text, symbol=data_symbol)
                if granularity == "monthly"
                else daily_range(start_text, end_text, symbol=data_symbol)
            )
            st.session_state["planned_data"] = {
                "start": start_text,
                "end": end_text,
                "symbol": data_symbol,
                "granularity": granularity,
                "max_transfer_mb": max_transfer_mb,
                "identity_keys": [item.key for item in identities],
            }
            st.session_state.pop("download_confirmed", None)
        except Exception as error:  # noqa: BLE001
            st.session_state.pop("planned_data", None)
            _show_error(root, "Intervalo não aceito pelo plano de backfill F5", error)

    planned_data = st.session_state.get("planned_data")
    if planned_data:
        try:
            identities = (
                month_range(planned_data["start"], planned_data["end"], symbol=planned_data["symbol"])
                if planned_data["granularity"] == "monthly"
                else daily_range(planned_data["start"], planned_data["end"], symbol=planned_data["symbol"])
            )
            count = len(identities)
            theoretical = count * (64 * 1024 * 1024 + MAX_CHECKSUM_BYTES)
            st.markdown(
                f"**Período:** {identities[0].period_start_utc[:10]} — "
                f"{(datetime.fromisoformat(identities[-1].period_end_utc.replace('Z', '+00:00')).date() - timedelta(days=1)).isoformat()} · "
                f"**arquivos:** {count} ZIP(s) + {count} CHECKSUM(s) · **limite:** {planned_data['max_transfer_mb']} MiB"
            )
            st.caption(
                f"Tamanho remoto não consultado. Pior caso teórico pelo limite por arquivo: {theoretical:,} bytes. "
                "O comando para de baixar antes do teto agregado."
            )
            plan_rows = []
            for identity in identities:
                local_match = next((
                    item for item in catalog.get("raw_objects", [])
                    if item.get("symbol") == identity.symbol
                    and item.get("year") == identity.year
                    and item.get("month") == identity.month
                    and (item.get("granularity") or "monthly") == identity.granularity
                    and item.get("day") == identity.day
                ), None)
                plan_rows.append({
                    "ZIP": identity.zip_name,
                    "URL": archive_url(identity),
                    "CHECKSUM": checksum_url(identity),
                    "estado_local": local_match.get("checksum_status", "não disponível") if local_match else "não disponível",
                    "bytes_locais": local_match.get("byte_size") if local_match else "desconhecido",
                })
            st.dataframe(pd.DataFrame(plan_rows), hide_index=True, width="stretch")
            confirmed = st.checkbox(
                f"Confirmo baixar {count} ZIP(s) de {planned_data['symbol']} ({planned_data['granularity']}) dentro do teto configurado.",
                key="download_confirmed",
            )
            if st.button("Baixar intervalo planejado", type="primary", disabled=not confirmed, key="fetch_confirmed"):
                remaining = planned_data["max_transfer_mb"] * 1024 * 1024
                completed = []
                try:
                    with st.spinner(f"Baixando até {count} arquivo(s), validando CHECKSUM e registrando revisões..."):
                        for identity in identities:
                            result, output = _capture_fetch(
                                identity,
                                root,
                                remaining,
                            )
                            completed.append({
                                "identity": identity.key,
                                "status": result.status,
                                "object_id": result.object_id,
                                "integrity_status": result.integrity_status,
                                "transferred_bytes": result.transferred_bytes,
                                "output": output.strip(),
                            })
                            remaining -= result.transferred_bytes
                    st.session_state["fetch_result"] = {"completed": completed, "remaining_bytes": remaining, "status": "COMPLETE"}
                    st.session_state.pop("planned_data", None)
                    st.rerun()
                except Exception as error:  # noqa: BLE001
                    consumed = int(getattr(error, "transferred_bytes", 0))
                    remaining = max(remaining - consumed, 0)
                    st.session_state["fetch_result"] = {
                        "completed": completed,
                        "remaining_bytes": remaining,
                        "status": "INTERRUPTED",
                        "failed_identity": identity.key if "identity" in locals() else "n/d",
                        "failed_transfer_bytes": consumed,
                    }
                    _show_error(root, "Backfill interrompido no teto ou por erro; ZIPs verificados antes da falha permanecem imutáveis", error)
        except Exception as error:  # noqa: BLE001
            _show_error(root, "Não foi possível reconstruir o plano", error)

    if fetch_result := st.session_state.get("fetch_result"):
        if fetch_result.get("status") == "INTERRUPTED":
            st.warning(
                f"Backfill interrompido em {fetch_result.get('failed_identity')}; "
                f"{len(fetch_result['completed'])} arquivo(s) concluído(s), "
                f"{fetch_result.get('failed_transfer_bytes', 0):,} bytes transferidos antes do erro, "
                f"{fetch_result['remaining_bytes']:,} bytes restantes."
            )
        else:
            st.success(f"Backfill concluído: {len(fetch_result['completed'])} arquivo(s) processados; {fetch_result['remaining_bytes']:,} bytes restantes.")
        st.dataframe(pd.DataFrame(fetch_result["completed"]), hide_index=True, width="stretch")

    st.divider()
    st.subheader("Raw, cobertura e QA")
    raw_rows = catalog.get("raw_objects", [])
    if raw_rows:
        st.dataframe(
            pd.DataFrame(raw_rows)[["object_id", "symbol", "granularity", "year", "month", "day", "acquisition_method", "source_url", "status", "checksum_status", "byte_size", "created_at_utc"]],
            hide_index=True,
            width="stretch",
        )
        raw_options = [item["object_id"] for item in raw_rows]
        selected_raw = st.selectbox(
            "Raw para auditoria",
            raw_options,
            format_func=lambda value: next(
                f"{item['symbol']} {item.get('granularity') or 'monthly'} {item['year']:04d}-{item['month']:02d}{('-' + str(item['day']).zfill(2)) if item.get('day') else ''} · {item['status']} · {value}"
                for item in raw_rows
                if item["object_id"] == value
            ),
            key="audit_raw_id",
        )
        if st.button("Auditar o raw selecionado", key="audit_raw"):
            try:
                with st.spinner("Normalizando o período e calculando QA..."):
                    reports = audit_raw_objects(root, raw_object_ids=[selected_raw])
                st.session_state["last_audit"] = reports[0]
                st.rerun()
            except Exception as error:  # noqa: BLE001
                _show_error(root, "Auditoria F2 falhou", error)
    else:
        st.info("Ainda não há ZIP importado. Envie um arquivo ou planeje um intervalo curto; depois volte aqui para auditar.")

    if catalog.get("partitions"):
        st.subheader("Partições curadas e qualidade")
        partition_view = [
            {
                "partition_id": item["partition_id"],
                "raw_object_id": item["raw_object_id"],
                "timeframe": item["timeframe"],
                "início UTC": item.get("first_open_at_utc"),
                "fim UTC": item.get("last_open_at_utc"),
                "QA": item["qa_status"],
                "observados": item["row_count"],
                "esperados": item["expected_count"],
                "gaps/bloqueios": item["gap_count"],
                "cobertura_pct": item["coverage_pct"],
            }
            for item in catalog["partitions"]
        ]
        st.dataframe(pd.DataFrame(partition_view), hide_index=True, width="stretch")
    else:
        st.info("Não há partições curadas. Importe um ZIP e execute a auditoria para ver cobertura, gaps e QA.")

    if audit := st.session_state.get("last_audit"):
        if audit.get("qa_status") == "PASS":
            st.success(f"QA {audit['qa_status']} · {audit.get('valid_rows', 0)} candles válidos · raw {audit.get('raw_object_id')}")
        else:
            st.error(f"QA {audit.get('qa_status', 'UNKNOWN')} · {audit.get('invalid_rows_count', 'n/a')} linha(s) inválida(s); consulte o relatório e a quarentena.")
        st.write(
            f"Fonte: {audit.get('source_url', 'arquivo importado')} · cobertura por partição; timestamps {audit.get('timestamp_unit', 'n/a')} · delay ASSUMED {audit.get('delay_assumed_ms', 'n/a')} ms."
        )
        if audit.get("partitions"):
            st.dataframe(pd.DataFrame(audit["partitions"]), hide_index=True, width="stretch")
        gaps = audit.get("missing_1m_ranges", [])
        if gaps:
            st.warning(f"Há {len(gaps)} intervalo(s) de gaps. As janelas atingidas são bloqueadas; não há preenchimento artificial.")
            with st.expander("Ver intervalos de gap"):
                st.dataframe(pd.DataFrame(gaps), hide_index=True, width="stretch")
        st.caption(f"Relatório QA: {audit.get('qa_json_path', 'indisponível')} · HTML {audit.get('qa_html_path', 'indisponível')}")

    st.subheader("Fixar snapshot explícito")
    pass_partitions = [item for item in catalog.get("partitions", []) if item.get("qa_status") == "PASS"]
    if pass_partitions:
        labels: dict[str, str] = {}
        for item in pass_partitions:
            option = (
                f"{item['timeframe']} · {item['symbol']} · {item['first_open_at_utc']} · "
                f"cobertura {item['coverage_pct']}% · gaps {item['gap_count']} · {item['partition_id']}"
            )
            labels[option] = item["partition_id"]
        selected_labels = st.multiselect(
            "Partições QA PASS exatas (inclua ao menos uma 1m; agregados são opcionais)",
            list(labels),
            help="A seleção é explícita. Evite escolher revisões duplicadas do mesmo mês/timeframe.",
            key="snapshot_partition_selection",
        )
        origin_choice = st.selectbox(
            "Origem do snapshot",
            ["Detectar apenas aquisição oficial", "REAL (origem verificada por você)", "SYNTHETIC"],
            help="Arquivos importados offline exigem classificação explícita. O CHECKSUM local prova correspondência dos bytes, não autentica quem o publicou.",
            key="snapshot_origin",
        )
        if st.button("Criar snapshot imutável", disabled=not selected_labels, type="primary", key="create_snapshot"):
            try:
                origin = {
                    "Detectar apenas aquisição oficial": None,
                    "REAL (origem verificada por você)": "REAL",
                    "SYNTHETIC": "SYNTHETIC",
                }[origin_choice]
                snapshot = create_snapshot(root, [labels[label] for label in selected_labels], data_origin=origin)
                st.session_state["last_snapshot_id"] = snapshot["snapshot_id"]
                st.success(f"Snapshot QA PASS criado: {snapshot['snapshot_id']} · origem {snapshot['data_origin']}")
                st.rerun()
            except Exception as error:  # noqa: BLE001
                _show_error(root, "Snapshot não foi criado; revise partições e origem", error)
    else:
        st.info("Depois de auditar um raw e obter partições QA PASS, selecione aqui as partições exatas do snapshot.")

    if snapshots := catalog.get("snapshots", []):
        st.subheader("Snapshots locais")
        view = [
            {key: snapshot.get(key) for key in ("snapshot_id", "symbol", "data_origin", "qa_status", "delay_assumed_ms", "source_partition_count", "created_at_utc")}
            for snapshot in snapshots
        ]
        st.dataframe(pd.DataFrame(view), hide_index=True, width="stretch")
        selected_snapshot_id = st.selectbox(
            "Proveniência de snapshot",
            [item["snapshot_id"] for item in snapshots],
            key="data_snapshot_detail",
        )
        selected_snapshot = next(item for item in snapshots if item["snapshot_id"] == selected_snapshot_id)
        if selected_snapshot.get("manifest"):
            with st.expander("Abrir manifest do snapshot", expanded=False):
                st.json(selected_snapshot["manifest"], expanded=False)
            st.download_button(
                "Baixar manifest do snapshot",
                data=(json.dumps(selected_snapshot["manifest"], indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
                file_name=f"{selected_snapshot_id}-snapshot.json",
                mime="application/json",
                width="stretch",
                on_click="ignore",
            )

    if errors := catalog.get("ingestion_errors", []):
        with st.expander("Eventos de ingestão recentes"):
            st.dataframe(pd.DataFrame(errors), hide_index=True, width="stretch")

    if catalog.get("catalog_error"):
        st.warning("O catálogo não pôde ser lido; nenhum dado foi alterado.")
        st.caption(catalog["catalog_error"])


def _downsample_equity(frame: pd.DataFrame, limit_per_strategy: int = 1500) -> pd.DataFrame:
    result = []
    for _, group in frame.groupby("strategy", sort=False):
        group = group.sort_values("timestamp_utc")
        if len(group) > limit_per_strategy:
            step = max(1, (len(group) - 1) // (limit_per_strategy - 1))
            group = pd.concat([group.iloc[::step], group.iloc[[-1]]]).drop_duplicates(subset=["timestamp_utc"])
        result.append(group)
    return pd.concat(result, ignore_index=True) if result else frame


def _render_run_charts(run: dict) -> None:
    equity_rows = run.get("equity", [])
    if not equity_rows:
        st.info("Este run não possui série de equity para desenhar.")
        return
    frame = pd.DataFrame(equity_rows)
    if not {"strategy", "timestamp_utc", "equity_usdt"}.issubset(frame.columns):
        st.info("O formato deste CSV não contém as colunas necessárias para a curva.")
        return
    frame["timestamp_utc"] = pd.to_datetime(frame["timestamp_utc"], utc=True, errors="coerce")
    frame["equity_usdt"] = pd.to_numeric(frame["equity_usdt"], errors="coerce")
    frame = frame.dropna(subset=["timestamp_utc", "equity_usdt"])
    if frame.empty:
        st.info("O CSV de equity está vazio ou inválido; consulte o run.json.")
        return

    plotted = _downsample_equity(frame)
    equity_fig = px.line(
        plotted,
        x="timestamp_utc",
        y="equity_usdt",
        color="strategy",
        render_mode="svg",
        labels={"timestamp_utc": "Tempo (UTC)", "equity_usdt": "Equity (USDT)", "strategy": "Série"},
        title="Equity marcada · inclui benchmark buy & hold",
    )
    equity_fig.update_layout(legend_title_text="Estratégia", margin=dict(l=8, r=8, t=52, b=8), height=360)
    st.plotly_chart(equity_fig, width="stretch", config={"scrollZoom": False})

    config = run["manifest"].get("config", {})
    selected = config.get("strategy")
    if not selected or selected not in set(frame["strategy"]):
        selected = "BUY_HOLD" if "BUY_HOLD" in set(frame["strategy"]) else str(frame["strategy"].iloc[0])
    curve = frame.loc[frame["strategy"] == selected, ["timestamp_utc", "equity_usdt"]].sort_values("timestamp_utc").copy()
    curve["peak_usdt"] = curve["equity_usdt"].cummax()
    curve["drawdown_pct"] = (curve["equity_usdt"] / curve["peak_usdt"] - 1) * 100
    drawdown_fig = px.area(
        curve,
        x="timestamp_utc",
        y="drawdown_pct",
        labels={"timestamp_utc": "Tempo (UTC)", "drawdown_pct": "Drawdown (%)"},
        title=f"Drawdown visualizado · {STRATEGY_LABELS.get(selected, selected)}",
    )
    drawdown_fig.update_layout(margin=dict(l=8, r=8, t=52, b=8), height=260, yaxis_ticksuffix="%")
    st.plotly_chart(drawdown_fig, width="stretch", config={"scrollZoom": False})
    st.caption("A curva é visualizada a partir de equity.csv; os pontos exibidos são reduzidos para manter a tela responsiva. O CSV mantém todas as marcações. O benchmark buy & hold usa o mesmo snapshot e a mesma política do run F3.")


def _render_run_summary(run: dict, *, show_extended_backtest_metrics: bool = False) -> None:
    if run.get("data_origin") == "SYNTHETIC":
        st.warning("DADOS SINTÉTICOS — demo de software; não é evidência de estratégia nem de lucro.")
    else:
        st.info(f"Origem {run.get('data_origin')} · evidência {run.get('evidence_level')}. Resultado histórico é exploratório e não demonstra rentabilidade futura.")
    st.markdown(f"**RUN_ID:** `{run['run_id']}` · fase {run.get('phase')} · criado {run.get('created_at_utc', 'n/d')}")
    manifest = run.get("manifest", {})
    snapshot = manifest.get("snapshot", {})
    dataset = manifest.get("dataset", {})
    symbol = snapshot.get("instrument_id") or dataset.get("instrument_id", "n/d")
    snapshot_id = snapshot.get("snapshot_id", "n/d")
    if snapshot_id != "n/d":
        st.caption(f"Instrumento: {symbol} · snapshot {snapshot_id} · QA {snapshot.get('qa_status', 'UNKNOWN')}")
    elif dataset:
        st.caption(f"Dataset da demo: {dataset.get('dataset_id', 'n/d')} · {symbol} · {dataset.get('candle_count', 'n/d')} candles")

    strategies = run.get("metrics", {}).get("strategies", {})
    if strategies:
        has_extended_backtest_metrics = show_extended_backtest_metrics and any(
            "daily_sharpe_observation_count" in metrics
            for metrics in strategies.values()
        )
        metric_rows = []
        for strategy, metrics in strategies.items():
            row = {
                "estratégia": STRATEGY_LABELS.get(strategy, strategy),
                "equity_final_usdt": metrics.get("final_equity_usdt"),
                "pnl_liquido_usdt": metrics.get("net_pnl_usdt", metrics.get("net_pnl_usdt")),
                "fees_usdt": metrics.get("total_fees_usdt"),
                "drawdown_max_pct": metrics.get("max_drawdown_pct"),
                "vetos": metrics.get("veto_count"),
            }
            if has_extended_backtest_metrics and "daily_sharpe_observation_count" in metrics:
                row.update({
                    "Sharpe_diario": (
                        metrics.get("daily_sharpe")
                        if metrics.get("daily_sharpe") is not None
                        else "N/D"
                    ),
                    "obs_diarias": metrics.get("daily_sharpe_observation_count", 0),
                    "win_rate_pct": (
                        metrics.get("win_rate_pct")
                        if metrics.get("win_rate_pct") is not None
                        else "N/D"
                    ),
                    "profit_factor": (
                        metrics.get("profit_factor")
                        if metrics.get("profit_factor") is not None
                        else "N/D"
                    ),
                    "trades_fechados": metrics.get("closed_trade_count", 0),
                })
            metric_rows.append(row)
        st.dataframe(pd.DataFrame(metric_rows), hide_index=True, width="stretch")
        if has_extended_backtest_metrics:
            st.caption("Sharpe usa retornos simples UTC de dias completos consecutivos, rf diário 0 e anualização √365; fica N/D com menos de duas observações ou variância zero. Win rate e profit factor consideram apenas trades fechados e custos líquidos; profit factor fica N/D sem perdas brutas.")
    _render_run_charts(run)
    st.caption(f"Diretório de artefatos: {run['directory']}")


def _render_backtest_page(root: Path, catalog: dict, runs: list[dict]) -> None:
    st.header("Backtests")
    st.write("Escolha um snapshot QA PASS. O painel chama o simulador F3 e grava um run reproduzível.")
    snapshots = [
        item for item in catalog.get("snapshots", [])
        if item.get("qa_status") == "PASS" and item.get("snapshot_id") not in catalog.get("sealed_holdout_ids", [])
    ]
    if not snapshots:
        if catalog.get("sealed_holdout_ids"):
            st.info("Os snapshots disponíveis estão selados para avaliação final; backtests exploratórios não podem acessá-los.")
        else:
            st.info("Nenhum snapshot QA PASS está disponível. Em Dados, importe, audite e fixe partições incluindo 1m.")
        return

    symbols = sorted({item["symbol"] for item in snapshots})
    symbol = st.selectbox("Símbolo Spot", symbols, key="backtest_symbol")
    eligible_snapshots = [item for item in snapshots if item["symbol"] == symbol]
    snapshot_ids = [item["snapshot_id"] for item in eligible_snapshots]
    snapshot_id = st.selectbox(
        "Snapshot imutável",
        snapshot_ids,
        format_func=lambda value: next(
            f"{value} · {item['data_origin']} · QA PASS"
            for item in eligible_snapshots
            if item["snapshot_id"] == value
        ),
        key="backtest_snapshot_id",
    )
    selected_snapshot = next(item for item in eligible_snapshots if item["snapshot_id"] == snapshot_id)
    manifest = selected_snapshot.get("manifest") or {}
    st.caption(f"Instrumento `BINANCE:SPOT:{symbol}` · delay herdado {selected_snapshot['delay_assumed_ms']} ms (ASSUMED) · {selected_snapshot['source_partition_count']} partições fixadas.")
    st.caption("As barras de decisão são agregadas causalmente dos candles 1m completos; 15m/1h/4h/1d ficam indisponíveis quando há lacunas ou cobertura insuficiente.")

    with st.form("backtest_form"):
        first, second = st.columns(2)
        with first:
            timeframe = st.selectbox("Timeframe de decisão", ["15m", "1h", "4h", "1d"], key="backtest_timeframe")
            strategy = st.selectbox(
                "Estratégia",
                ["CASH", "BUY_HOLD", "SMA_CROSS", "BREAKOUT", "MOMENTUM", "MEAN_REVERSION"],
                format_func=lambda value: STRATEGY_LABELS.get(value, value),
                key="backtest_strategy",
            )
            capital = st.number_input("Capital inicial (USDT)", min_value=1.0, value=1000.0, step=100.0, key="backtest_capital")
        with second:
            fee_bps = st.number_input("Fee hipotética por lado (bps)", min_value=0.0, value=10.0, step=1.0, key="backtest_fee")
            slippage_bps = st.number_input("Slippage proxy hipotética por lado (bps)", min_value=0.0, value=5.0, step=1.0, key="backtest_slippage")
            default_delay = int(manifest.get("delay_assumed_ms", selected_snapshot.get("delay_assumed_ms", 1000)))
            delay_ms = st.number_input("Atraso assumido (ms)", min_value=1, max_value=300000, value=max(default_delay, 1), step=100, help="Hipótese de disponibilidade; não é latência observada.", key="backtest_delay")

        sma_fast = 20
        sma_slow = 50
        breakout_lookback = 20
        momentum_lookback = 24
        mean_reversion_lookback = 30
        mean_reversion_entry_z = Decimal("-2")
        mean_reversion_exit_z = Decimal("0")
        if strategy == "SMA_CROSS":
            param_a, param_b = st.columns(2)
            sma_fast = int(param_a.number_input("SMA rápida", min_value=1, max_value=500, value=20, step=1, key="sma_fast"))
            sma_slow = int(param_b.number_input("SMA lenta", min_value=2, max_value=1000, value=50, step=1, key="sma_slow"))
        elif strategy == "BREAKOUT":
            breakout_lookback = int(st.number_input("Lookback de breakout", min_value=2, max_value=1000, value=20, step=1, key="breakout_lookback"))
        elif strategy == "MOMENTUM":
            momentum_lookback = int(st.number_input("Lookback de momentum (barras)", min_value=1, max_value=5000, value=24, step=1, key="backtest_momentum_lookback"))
        elif strategy == "MEAN_REVERSION":
            param_a, param_b, param_c = st.columns(3)
            mean_reversion_lookback = int(param_a.number_input("Lookback z-score (barras)", min_value=2, max_value=5000, value=30, step=1, key="backtest_mean_reversion_lookback"))
            mean_reversion_entry_z = Decimal(str(param_b.number_input("Entrada z ≤", min_value=-20.0, max_value=20.0, value=-2.0, step=0.1, key="backtest_mean_reversion_entry_z")))
            mean_reversion_exit_z = Decimal(str(param_c.number_input("Saída z ≥", min_value=-20.0, max_value=20.0, value=0.0, step=0.1, key="backtest_mean_reversion_exit_z")))
        run_requested = st.form_submit_button("Executar backtest", type="primary")

    st.warning("Fees e slippage são custos hipotéticos configurados; o slippage é aplicado ao open de referência, sem somar spread separado. Candle OHLCV não garante fill real. Sem short, margem, alavancagem ou ordens reais.")
    if run_requested:
        try:
            ensure_snapshot_available(root, snapshot_id)
            snapshot = load_backtest_snapshot(root, snapshot_id)
            config = BacktestConfig(
                strategy=StrategyName(strategy),
                decision_timeframe=timeframe,
                initial_cash_usdt=Decimal(str(capital)),
                fee_bps_per_side=Decimal(str(fee_bps)),
                slippage_bps_per_side=Decimal(str(slippage_bps)),
                assumed_data_delay_seconds=Decimal(int(delay_ms)) / Decimal("1000"),
                sma_fast=sma_fast,
                sma_slow=sma_slow,
                breakout_lookback=breakout_lookback,
                momentum_lookback=momentum_lookback,
                mean_reversion_lookback=mean_reversion_lookback,
                mean_reversion_entry_z=mean_reversion_entry_z,
                mean_reversion_exit_z=mean_reversion_exit_z,
            )
            with st.spinner("Executando o motor F3 e escrevendo os artefatos..."):
                result = run_backtest(snapshot, config)
                artifacts = write_backtest_artifacts(root / "artifacts" / "backtests", result)
            st.session_state["selected_run_id"] = artifacts["run_id"]
            st.success(f"Backtest concluído. RUN_ID: {artifacts['run_id']}")
            st.caption(f"Relatório: {artifacts['report_html']} · JSON: {artifacts['metrics_json']}")
            st.rerun()
        except Exception as error:  # noqa: BLE001
            _show_error(root, "Backtest não concluído; os runs anteriores foram preservados", error)

    selected_run_id = st.session_state.get("selected_run_id")
    if selected_run_id and any(item["run_id"] == selected_run_id for item in runs):
        try:
            _render_run_summary(
                load_run(root, selected_run_id),
                show_extended_backtest_metrics=True,
            )
        except Exception as error:  # noqa: BLE001
            _show_error(root, "Não foi possível carregar o run selecionado", error)
    elif not runs:
        st.info("Depois de executar, o RUN_ID aparecerá aqui e ficará em Runs anteriores.")


def _operation_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _render_operations_page(root: Path, runs: list[dict]) -> None:
    st.header("Operações")
    if not runs:
        st.info("Ainda não há operações salvas. Execute a demo ou um backtest em Dados/Backtests.")
        return
    options = [item["run_id"] for item in runs]
    default_index = next((index for index, value in enumerate(options) if value == st.session_state.get("selected_run_id")), 0)
    run_id = st.selectbox("Run", options, index=default_index, key="operations_run_id")
    try:
        run = load_run(root, run_id)
    except Exception as error:  # noqa: BLE001
        _show_error(root, "Não foi possível abrir o run", error)
        return
    if run["data_origin"] == "SYNTHETIC":
        st.warning("DADOS SINTÉTICOS — a demo contém fills de exercício e não representa mercado real.")

    operations = run.get("operations", [])
    if not operations:
        st.info("Este run não possui sinais ou fills. Abra outro RUN_ID ou execute um backtest com dados suficientes.")
        return

    strategies = sorted({item.get("strategy", "UNKNOWN") for item in operations})
    default_strategy = run.get("manifest", {}).get("config", {}).get("strategy")
    filter_options = ["Todas", *strategies]
    default_filter = strategies.index(default_strategy) + 1 if default_strategy in strategies else 0
    strategy_filter = st.selectbox(
        "Filtrar estratégia",
        filter_options,
        index=default_filter,
        format_func=lambda value: "Todas" if value == "Todas" else STRATEGY_LABELS.get(value, value),
        key="operations_strategy",
    )
    dates = [value for item in operations for value in (_operation_date(item.get("signal_at_utc", "")), _operation_date(item.get("fill_at_utc", ""))) if value]
    if dates:
        date_range = st.date_input("Período do sinal/fill (UTC)", value=(min(dates), max(dates)), key="operations_dates")
        if isinstance(date_range, tuple) and len(date_range) == 2:
            date_from, date_to = date_range
        elif isinstance(date_range, date):
            date_from = date_to = date_range
        else:
            date_from = date_to = None
    else:
        date_from = date_to = None
        st.caption("Datas indisponíveis nos artefatos deste run.")

    filtered = []
    for item in operations:
        if strategy_filter != "Todas" and item.get("strategy") != strategy_filter:
            continue
        timestamp = _operation_date(item.get("signal_at_utc", "")) or _operation_date(item.get("fill_at_utc", ""))
        if date_from and timestamp and timestamp < date_from:
            continue
        if date_to and timestamp and timestamp > date_to:
            continue
        filtered.append(item)

    if not filtered:
        st.info("Nenhuma linha corresponde aos filtros. Amplie o período ou selecione Todas.")
        return
    visible = [{column: item.get(column, "") for column in OPERATION_COLUMNS} for item in filtered]
    st.dataframe(pd.DataFrame(visible), hide_index=True, width="stretch")
    st.caption("P&L realizado aparece na saída que fecha uma posição e já inclui as fees de entrada/saída; posição aberta não tem P&L realizado. Campos vazios significam que o artefato não contém esse dado (runs F0 não gravam horários de sinal).")

    operation_ids = [item["operation_id"] for item in filtered]
    operation_by_id = {item["operation_id"]: item for item in filtered}
    selected_operation = st.selectbox(
        "Selecionar operação para ver detalhes",
        operation_ids,
        format_func=lambda operation_id: (
            f"{operation_by_id[operation_id].get('strategy')} · {operation_by_id[operation_id].get('side') or operation_by_id[operation_id].get('execution_status')} · "
            f"sinal {operation_by_id[operation_id].get('signal_at_utc') or 'n/d'} · fill {operation_by_id[operation_id].get('fill_at_utc') or 'n/d'}"
        ),
        key=f"selected_operation_{run_id}",
    )
    st.json(operation_by_id[selected_operation], expanded=True)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=OPERATION_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(visible)
    st.download_button(
        "Baixar operações filtradas em CSV",
        data=output.getvalue().encode("utf-8-sig"),
        file_name=f"{run_id}-operacoes.csv",
        mime="text/csv",
        width="stretch",
        on_click="ignore",
    )


def _render_comparison_page(root: Path, runs: list[dict]) -> None:
    st.header("Comparar runs")
    st.write("Selecione de dois a quatro runs F3/F5. Diferenças de ativo, período, snapshot ou execução ficam visíveis.")
    eligible = [item for item in runs if item.get("phase") in {"F3", "F5_HOLDOUT", "F6_ML"}]
    if len(eligible) < 2:
        st.info("Salve ao menos dois runs para comparar custos, risco, exposição e turnover.")
        return
    options = [item["run_id"] for item in eligible]
    selected_ids = st.multiselect(
        "Runs (2–4)",
        options,
        format_func=lambda value: next(
            f"{item['created_at_utc']} · {item['evidence_level']} · {value}" for item in eligible if item["run_id"] == value
        ),
        max_selections=4,
        key="comparison_run_ids",
    )
    if len(selected_ids) < 2:
        st.info("Escolha pelo menos dois runs.")
        return
    loaded = [load_run(root, run_id) for run_id in selected_ids]
    manifests = [item["manifest"] for item in loaded]
    snapshots = [manifest.get("snapshot", {}) for manifest in manifests]
    periods = []
    for manifest, snapshot in zip(manifests, snapshots, strict=True):
        window = (manifest.get("audit") or {}).get("evaluation_window") or {}
        periods.append((
            window.get("start_at_utc") or snapshot.get("start_open_at_utc"),
            window.get("end_at_utc_exclusive") or snapshot.get("end_at_utc"),
        ))
    policies = [manifest.get("execution_policy", {}) for manifest in manifests]
    comparisons = {
        "Instrumentos": {item.get("instrument_id") for item in snapshots},
        "Períodos avaliados": set(periods),
        "Snapshots": {item.get("snapshot_id") for item in snapshots},
        "Execução e capital": {
            json.dumps(
                {
                    "execution_policy": {key: policy.get(key) for key in (
                        "market", "position_mode", "assumed_data_delay_ms", "fill_on",
                        "fee_bps_per_side", "slippage_proxy_bps_per_side", "spread_added_separately",
                    )},
                    "initial_cash_usdt": (manifest.get("config") or {}).get("initial_cash_usdt"),
                },
                sort_keys=True,
            )
            for manifest, policy in zip(manifests, policies, strict=True)
        },
    }
    mismatches = [label for label, values in comparisons.items() if len(values) > 1]
    if mismatches:
        st.error("Runs incompatíveis em: " + ", ".join(mismatches) + ". Os resultados abaixo mostram as diferenças sem afirmar períodos equivalentes.")
    else:
        st.success("Instrumento, período, snapshot e política de execução coincidem.")
    rows: list[dict] = []
    chart_rows: list[dict] = []
    for run in loaded:
        manifest = run["manifest"]
        snapshot = manifest.get("snapshot", {})
        strategy_spec = manifest.get("strategy") or {}
        strategy = strategy_spec.get("name", "")
        config = manifest.get("config") or {}
        item_metrics = run.get("metrics", {}).get("strategies", {}).get(strategy, {})
        policy = manifest.get("execution_policy", {})
        rows.append({
            "RUN_ID": run["run_id"],
            "evidência": manifest.get("evidence_level"),
            "instrumento": snapshot.get("instrument_id"),
            "avaliação inicial UTC": periods[len(rows)][0],
            "avaliação final UTC": periods[len(rows)][1],
            "cobertura inicial UTC": snapshot.get("start_open_at_utc"),
            "cobertura final UTC": snapshot.get("end_at_utc"),
            "snapshot": snapshot.get("snapshot_id"),
            "estratégia": strategy,
            "timeframe": strategy_spec.get("decision_timeframe"),
            "parâmetros": json.dumps(strategy_spec.get("parameters", {}), sort_keys=True, ensure_ascii=False),
            "config SHA-256": (manifest.get("config_hashes") or {}).get("config_sha256"),
            "capital inicial USDT": config.get("initial_cash_usdt"),
            "fee bps/lado": policy.get("fee_bps_per_side"),
            "slippage bps/lado": policy.get("slippage_proxy_bps_per_side"),
            "retorno líquido %": item_metrics.get("net_return_pct"),
            "drawdown máx. %": item_metrics.get("max_drawdown_pct"),
            "exposição %": item_metrics.get("exposure_pct"),
            "turnover USDT": item_metrics.get("turnover_quote_usdt"),
            "fees USDT": item_metrics.get("total_fees_usdt"),
            "slippage USDT": item_metrics.get("total_slippage_cost_usdt"),
        })
        for point in run.get("equity", []):
            if point.get("strategy") == strategy:
                chart_rows.append({
                    "run_id": run["run_id"],
                    "timestamp_utc": point.get("timestamp_utc"),
                    "equity_usdt": float(point.get("equity_usdt") or 0),
                })
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    if chart_rows:
        st.plotly_chart(
            px.line(pd.DataFrame(chart_rows), x="timestamp_utc", y="equity_usdt", color="run_id", title="Equity por run"),
            width="stretch",
        )


def _make_ui_config(spec: dict, overrides: dict[str, int] | None = None) -> BacktestConfig:
    base = dict(spec["base_config"])
    base.update(overrides or {})
    return BacktestConfig(
        strategy=StrategyName(str(base["strategy"]).upper()),
        decision_timeframe=str(base["decision_timeframe"]),
        initial_cash_usdt=Decimal(str(base["initial_cash_usdt"])),
        fee_bps_per_side=Decimal(str(base["fee_bps_per_side"])),
        slippage_bps_per_side=Decimal(str(base["slippage_bps_per_side"])),
        assumed_data_delay_seconds=Decimal(str(base["assumed_data_delay_seconds"])),
        sma_fast=int(base["sma_fast"]),
        sma_slow=int(base["sma_slow"]),
        breakout_lookback=int(base["breakout_lookback"]),
        momentum_lookback=int(base.get("momentum_lookback", 24)),
        mean_reversion_lookback=int(base.get("mean_reversion_lookback", 30)),
        mean_reversion_entry_z=Decimal(str(base.get("mean_reversion_entry_z", "-2"))),
        mean_reversion_exit_z=Decimal(str(base.get("mean_reversion_exit_z", "0"))),
    )


def _render_research_page(root: Path, catalog: dict) -> None:
    st.header("Pesquisa F5")
    st.write("Registre hipótese, parâmetros, tentativas, snapshot, métrica primária e split antes de avaliar resultados.")
    if not catalog.get("snapshots"):
        st.info("Crie snapshots QA PASS em Dados antes de registrar uma pesquisa.")
        return
    try:
        experiments = list_experiments(root)
    except Exception as error:  # noqa: BLE001
        _show_error(root, "Registro de pesquisa indisponível", error)
        return
    good_snapshots = [item for item in catalog["snapshots"] if item.get("qa_status") == "PASS"]
    available_development = [
        item for item in good_snapshots
        if item["snapshot_id"] not in catalog.get("sealed_holdout_ids", [])
    ]
    if not experiments:
        st.caption("Nenhuma versão de experimento registrada. O critério será salvo antes do primeiro trial.")
    with st.expander("Registrar uma versão de experimento", expanded=not experiments):
        if not available_development:
            st.warning("Não há snapshot QA PASS disponível para desenvolvimento.")
        strategy = st.selectbox(
            "Regra F3",
            ["SMA_CROSS", "BREAKOUT", "MOMENTUM", "MEAN_REVERSION", "BUY_HOLD", "CASH"],
            key="research_strategy",
        )
        timeframe = st.selectbox("Timeframe das barras", ["15m", "1h", "4h", "1d"], key="research_timeframe")
        with st.form("research_register_form"):
            family_id = st.text_input("Família da hipótese", value="sma-risk-study")
            version = int(st.number_input("Versão da família", min_value=1, value=1, step=1))
            development_id = st.selectbox(
                "Snapshot de desenvolvimento (seleção/tuning)",
                [item["snapshot_id"] for item in available_development] or [""],
                format_func=lambda value: next(
                    f"{item['symbol']} · {value} · {item['data_origin']}"
                    for item in available_development if item["snapshot_id"] == value
                ) if value else "Sem snapshot",
            )
            left, right = st.columns(2)
            with left:
                cash = st.number_input("Capital inicial (USDT)", min_value=1.0, value=1000.0, step=100.0)
                fee = st.number_input("Fee hipotética (bps/lado)", min_value=0.0, value=10.0, step=1.0)
                slippage = st.number_input("Slippage proxy (bps/lado)", min_value=0.0, value=5.0, step=1.0)
            with right:
                sma_fast = int(st.number_input("SMA rápida", min_value=1, max_value=500, value=20, step=1))
                sma_slow = int(st.number_input("SMA lenta", min_value=2, max_value=1000, value=50, step=1))
                breakout_lookback = int(st.number_input("Lookback breakout", min_value=1, max_value=1000, value=20, step=1))
                momentum_lookback = int(st.number_input("Lookback momentum", min_value=1, max_value=1000, value=24, step=1))
                mean_reversion_lookback = int(st.number_input("Janela mean reversion", min_value=2, max_value=1000, value=30, step=1))
                mean_reversion_entry_z = Decimal(str(st.number_input("Z de entrada mean reversion", value=-2.0, step=0.1, format="%.2f")))
                mean_reversion_exit_z = Decimal(str(st.number_input("Z de saída mean reversion", value=0.0, step=0.1, format="%.2f")))
            allowed: dict[str, list[int]] = {}
            if strategy == "SMA_CROSS":
                fast_choices = [value for value in (5, 10, 15, 20, 25, 30, 40, 50, 100) if value <= 500]
                slow_choices = [value for value in (20, 30, 40, 50, 60, 100, 150, 200) if value <= 1000]
                allowed = {
                    "sma_fast": sorted(set(st.multiselect("Valores permitidos SMA rápida", fast_choices + [sma_fast], default=[sma_fast]))),
                    "sma_slow": sorted(set(st.multiselect("Valores permitidos SMA lenta", slow_choices + [sma_slow], default=[sma_slow]))),
                }
            elif strategy == "BREAKOUT":
                breakout_choices = sorted(set([5, 10, 20, 30, 50, 100, breakout_lookback]))
                allowed = {
                    "breakout_lookback": sorted(set(st.multiselect(
                        "Valores permitidos de lookback",
                        breakout_choices,
                        default=[breakout_lookback],
                    )))
                }
            elif strategy == "MOMENTUM":
                momentum_choices = sorted(set([12, 24, 48, 96, momentum_lookback]))
                allowed = {
                    "momentum_lookback": sorted(set(st.multiselect(
                        "Valores permitidos de lookback momentum",
                        momentum_choices,
                        default=[momentum_lookback],
                    )))
                }
            elif strategy == "MEAN_REVERSION":
                mean_reversion_choices = sorted(set([20, 30, 45, 60, mean_reversion_lookback]))
                allowed = {
                    "mean_reversion_lookback": sorted(set(st.multiselect(
                        "Valores permitidos da janela mean reversion",
                        mean_reversion_choices,
                        default=[mean_reversion_lookback],
                    )))
                }
            else:
                allowed = {}
            st.markdown("**Critério científico registrado antes dos trials**")
            metric = st.selectbox(
                "Métrica primária",
                list(METRIC_DIRECTIONS),
                format_func=lambda value: {
                    "net_return_pct": "Retorno líquido % (maximizar)",
                    "max_drawdown_pct": "Drawdown máximo % (minimizar)",
                    "exposure_pct": "Exposição % (minimizar)",
                    "turnover_quote_usdt": "Turnover USDT (minimizar)",
                    "daily_sharpe": "Sharpe diário (maximizar; √365, RF=0)",
                }[value],
            )
            default_threshold = 30.0 if metric == "max_drawdown_pct" else 0.0
            threshold = Decimal(str(st.number_input("Limiar pré-registrado", value=default_threshold, step=1.0)))
            col_a, col_b = st.columns(2)
            with col_a:
                max_attempts = int(st.number_input("Orçamento máximo de tentativas", min_value=1, max_value=50, value=5, step=1))
                train_bars = int(st.number_input(f"Barras de treino ({timeframe})", min_value=2, max_value=10000, value=48, step=1))
                test_bars = int(st.number_input("Barras por teste", min_value=1, max_value=10000, value=12, step=1))
            with col_b:
                max_folds = int(st.number_input("Máximo de folds", min_value=1, max_value=5, value=3, step=1))
                minimum_folds = int(st.number_input("Folds mínimos para decisão", min_value=1, max_value=5, value=2, step=1))
                holdout_start = st.text_input("Início do holdout UTC ISO", value="2026-07-01T00:00:00Z")
                holdout_end = st.text_input("Fim exclusivo do holdout UTC ISO", value="2026-10-29T00:00:00Z")
            with st.expander("Campos para modelos futuros; não aplicados em F5"):
                horizon_enabled = st.checkbox("Registrar horizon do label", value=False)
                label_horizon = int(st.number_input(
                    "Horizon em barras",
                    min_value=1,
                    value=1,
                    step=1,
                    disabled=not horizon_enabled,
                ))
                purge_bars = int(st.number_input("Purge em barras", min_value=0, value=0, step=1))
            development_symbol = next(
                (item["symbol"] for item in good_snapshots if item["snapshot_id"] == development_id),
                None,
            )
            st.caption("O snapshot holdout precisa usar o mesmo instrumento e cobrir uma janela posterior completa.")
            holdout_choices = ["Manter selado sem snapshot"] + [
                item["snapshot_id"] for item in good_snapshots
                if item["symbol"] == development_symbol
                if item["snapshot_id"] != development_id
                and item["snapshot_id"] not in catalog.get("sealed_holdout_ids", [])
            ]
            holdout_choice = st.selectbox("Snapshot final (opcional; precisa cobrir o período e warm-up)", holdout_choices)
            confirm = st.checkbox("Confirmo que hipótese, métrica, limiar, períodos e orçamento estão registrados antes dos resultados.")
            create_requested = st.form_submit_button("Registrar contrato de pesquisa", disabled=not confirm or not development_id)
        if create_requested:
            try:
                selected_snapshot = next(item for item in good_snapshots if item["snapshot_id"] == development_id)
                delay = Decimal(str((selected_snapshot.get("manifest") or {}).get("delay_assumed_ms", 1000))) / Decimal("1000")
                config = BacktestConfig(
                    strategy=StrategyName(strategy),
                    decision_timeframe=timeframe,
                    initial_cash_usdt=Decimal(str(cash)),
                    fee_bps_per_side=Decimal(str(fee)),
                    slippage_bps_per_side=Decimal(str(slippage)),
                    assumed_data_delay_seconds=delay,
                    sma_fast=sma_fast,
                    sma_slow=sma_slow,
                    breakout_lookback=breakout_lookback,
                    momentum_lookback=momentum_lookback,
                    mean_reversion_lookback=mean_reversion_lookback,
                    mean_reversion_entry_z=mean_reversion_entry_z,
                    mean_reversion_exit_z=mean_reversion_exit_z,
                )
                created = create_experiment(
                    root,
                    family_id=family_id,
                    version=version,
                    development_snapshot_id=development_id,
                    config=config,
                    max_attempts=max_attempts,
                    train_bars=train_bars,
                    test_bars=test_bars,
                    max_folds=max_folds,
                    primary_metric=metric,
                    primary_direction=METRIC_DIRECTIONS[metric],
                    primary_threshold=threshold,
                    minimum_test_folds=minimum_folds,
                    holdout_start_at_utc=holdout_start,
                    holdout_end_at_utc=holdout_end,
                    holdout_snapshot_id=None if holdout_choice == holdout_choices[0] else holdout_choice,
                    allowed_parameter_values=allowed,
                    label_horizon=label_horizon if horizon_enabled else None,
                    purge_bars=purge_bars,
                )
                st.success(f"Contrato {created['experiment_id']} registrado · spec SHA-256 {created['spec_sha256']} · holdout SELADO.")
                st.rerun()
            except Exception as error:  # noqa: BLE001
                _show_error(root, "Experimento não registrado; nenhum resultado foi aberto", error)

    if not experiments:
        return
    selected_id = st.selectbox("Versão de pesquisa", [item["experiment_id"] for item in experiments], key="research_experiment_id")
    current = next(item for item in experiments if item["experiment_id"] == selected_id)
    spec = current["spec"]
    development_snapshot = next(
        (item for item in good_snapshots if item["snapshot_id"] == current["development_snapshot_id"]),
        None,
    )
    if development_snapshot and development_snapshot.get("manifest"):
        source_rows = development_snapshot["manifest"].get("sources", [])
        if source_rows:
            st.subheader("Cobertura registrada do snapshot de desenvolvimento")
            coverage_view = [
                {
                    "arquivo": row.get("source_archive_name"),
                    "granularidade": row.get("source_granularity"),
                    "revisão raw": row.get("raw_revision"),
                    "início UTC": row.get("first_open_at_utc"),
                    "fim UTC": row.get("last_open_at_utc"),
                    "lacunas": row.get("gap_count"),
                    "cobertura %": row.get("coverage_pct"),
                }
                for row in source_rows
            ]
            st.dataframe(pd.DataFrame(coverage_view), hide_index=True, width="stretch")
            excluded = development_snapshot["manifest"].get("excluded_overlapping_partitions", [])
            if excluded:
                st.warning(
                    f"O snapshot registrou {len(excluded)} partição(ões) diária(s)/mensal(is) ou revisão(ões) sobrepostas excluídas da seleção explícita."
                )
    st.markdown(
        f"**Status:** {current['status']} · **snapshot de desenvolvimento:** {current['development_snapshot_id']} · "
        f"**holdout:** {current['holdout_snapshot_id'] or 'SELADO — snapshot ainda indisponível'}"
    )
    st.caption(
        f"Spec SHA-256: {current['spec_sha256']} · métrica primária {spec['primary_metric']} "
        f"{spec['primary_direction']} {spec['primary_threshold']} · orçamento {spec['max_attempts']} tentativas."
    )
    st.json({
        "hipótese/família": f"{current['family_id']} v{current['version']}",
        "seleção registrada": spec["base_config"],
        "split": spec["walk_forward"],
        "holdout selado": spec["holdout_period"],
    }, expanded=False)

    if current["status"] == "OPEN" and not current["holdout_snapshot_id"]:
        current_development = next(
            (item for item in good_snapshots if item["snapshot_id"] == current["development_snapshot_id"]),
            None,
        )
        development_symbol = current_development.get("symbol") if current_development else None
        eligible = [
            item["snapshot_id"] for item in good_snapshots
            if item["symbol"] == development_symbol
            if item["snapshot_id"] != current["development_snapshot_id"]
            and item["snapshot_id"] not in catalog.get("sealed_holdout_ids", [])
        ]
        if eligible:
            bind_choice = st.selectbox("Vincular snapshot final antes de congelar", eligible, key=f"bind_{selected_id}")
            if st.button("Vincular e manter holdout selado", key=f"bind_button_{selected_id}"):
                try:
                    bind_holdout_snapshot(root, selected_id, bind_choice)
                    st.success("Snapshot vinculado como holdout selado; backtests comuns estão bloqueados para ele.")
                    st.rerun()
                except Exception as error:  # noqa: BLE001
                    _show_error(root, "Snapshot não lacrado; confira cobertura completa e warm-up", error)
        else:
            st.info("Nenhum snapshot separado está disponível. O período permanece registrado e selado.")

    trials = current.get("trials", [])
    if current["status"] == "OPEN":
        overrides: dict[str, int] = {}
        with st.form(f"trial_form_{selected_id}"):
            st.subheader("Registrar trial")
            for key, values in spec["allowed_parameter_values"].items():
                base_value = int(spec["base_config"][key])
                choices = sorted(set([base_value, *[int(value) for value in values]]))
                overrides[key] = int(st.selectbox(
                    f"{key} pré-registrado",
                    choices,
                    index=choices.index(base_value),
                    key=f"trial_param_{selected_id}_{key}",
                ))
            run_trial = st.form_submit_button("Executar um trial e salvar folds")
        if run_trial:
            try:
                with st.spinner("Executando folds cronológicos e custos 2x/3x; gravando manifest..."):
                    manifest = record_trial(root, selected_id, _make_ui_config(spec, overrides))
                st.success(f"{manifest['trial_id']} salvo · {manifest['decision']['status']} · folds {len(manifest['folds'])} · hash {manifest['config_sha256']}")
                st.rerun()
            except Exception as error:  # noqa: BLE001
                _show_error(root, "Trial não concluído; consulte os eventos de auditoria", error)

    if trials:
        st.subheader("Trials registrados")
        st.dataframe(pd.DataFrame(trials), hide_index=True, width="stretch")
        complete_trials = [item for item in trials if item["status"] == "COMPLETE"]
        if complete_trials:
            trial_ids = [item["trial_id"] for item in complete_trials]
            selected_trial = st.selectbox("Manifest de trial", trial_ids, key=f"trial_manifest_{selected_id}")
            try:
                manifest = load_trial_manifest(root, selected_trial)
                st.markdown(
                    f"**Selecionado:** {manifest['config']['strategy']} · parâmetros {manifest['config']} · "
                    f"estado {manifest['decision']['status']} · holdout SELADO"
                )
                st.dataframe(pd.DataFrame([
                    {
                        "fold": item["fold"],
                        "treino": f"{item['train_start_at_utc']} — {item['train_end_at_utc_exclusive']}",
                        "teste": f"{item['test_start_at_utc']} — {item['test_end_at_utc_exclusive']}",
                        "retorno teste %": item["test"]["strategy"]["net_return_pct"],
                        "drawdown %": item["test"]["strategy"]["max_drawdown_pct"],
                        "Sharpe diário": item["test"]["strategy"].get("daily_sharpe"),
                        "win rate %": item["test"]["strategy"].get("win_rate_pct"),
                        "profit factor": item["test"]["strategy"].get("profit_factor"),
                        "retorno 3x custos %": item["cost_stress"]["3x"]["metrics"]["net_return_pct"],
                    }
                    for item in manifest["folds"]
                ]), hide_index=True, width="stretch")
                st.caption(f"Manifest: artifacts/research/{selected_id}/{selected_trial}/trial.json · config SHA-256 {manifest['config_sha256']}")
                coverage = manifest.get("development_snapshot", {})
                if coverage.get("missing_1m_minutes_in_partitions"):
                    st.warning(
                        f"Cobertura do snapshot contém {coverage['missing_1m_minutes_in_partitions']} minuto(s) ausente(s) "
                        f"em partições; cobertura mínima {coverage.get('minimum_partition_coverage_pct')}%. Consulte as exclusões de segmentos e folds."
                    )
                st.download_button(
                    "Baixar manifest do trial",
                    data=(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
                    file_name=f"{selected_trial}.json",
                    mime="application/json",
                    key=f"download_trial_{selected_id}",
                )
            except Exception as error:  # noqa: BLE001
                _show_error(root, "Não foi possível abrir o manifest do trial", error)
            if current["status"] == "OPEN":
                freeze_trial = st.selectbox(
                    "Trial selecionado para congelamento",
                    trial_ids,
                    key=f"freeze_trial_{selected_id}",
                )
                if st.button("Congelar família/versão", type="primary", key=f"freeze_{selected_id}"):
                    try:
                        freeze_experiment(root, selected_id, freeze_trial)
                        st.success("Versão congelada; novos trials estão bloqueados.")
                        st.rerun()
                    except Exception as error:  # noqa: BLE001
                        _show_error(root, "Versão não congelada", error)

    if current["status"] == "FROZEN":
        st.warning("Versão congelada. O holdout continua SELADO até a avaliação formal única.")
        if current["holdout_snapshot_id"]:
            confirm_open = st.checkbox("Confirmo abrir o holdout uma única vez.", key=f"confirm_holdout_{selected_id}")
            if st.button("Abrir avaliação final", type="primary", disabled=not confirm_open, key=f"open_holdout_{selected_id}"):
                try:
                    result = open_holdout(root, selected_id)
                    st.success(f"Holdout consultado uma vez · run {result['backtest_run_id']} · SEALED_HOLDOUT.")
                    st.json({
                        "estratégia congelada": result["metrics"],
                        "buy and hold": result["buy_hold_metrics"],
                        "custos 2x e 3x": result["cost_stress"],
                    }, expanded=True)
                    st.rerun()
                except Exception as error:  # noqa: BLE001
                    _show_error(root, "Avaliação final registrada; a versão permanecerá bloqueada", error)
        else:
            st.info("Sem snapshot com cobertura completa: período final continua selado.")
    if current["status"] == "HOLDOUT_OPENED":
        st.error("HOLDOUT ABERTO UMA VEZ · tuning desta versão está permanentemente bloqueado.")
        manifest_path = root / "artifacts" / "research" / selected_id / "holdout.json"
        if manifest_path.is_file():
            st.json(json.loads(manifest_path.read_text(encoding="utf-8")), expanded=True)
        elif current.get("holdout_run_id"):
            st.caption(f"Run formal: {current['holdout_run_id']}")
    with st.expander("Eventos de auditoria da pesquisa"):
        try:
            events = list_research_events(root, selected_id)
            st.dataframe(pd.DataFrame([
                {
                    "event_id": item["event_id"],
                    "evento": item["event_type"],
                    "horário UTC": item["occurred_at_utc"],
                    "detalhes": json.dumps(item["details"], ensure_ascii=False, sort_keys=True),
                }
                for item in events
            ]), hide_index=True, width="stretch")
        except Exception as error:  # noqa: BLE001
            _show_error(root, "Eventos de auditoria indisponíveis", error)


def _render_history_page(root: Path, runs: list[dict]) -> None:
    st.header("Runs anteriores")
    if not runs:
        st.info("Nenhum run foi salvo ainda. Em Dados, execute a demo sintética; ou rode um backtest a partir de um snapshot.")
        return
    options = [item["run_id"] for item in runs]
    default_index = next((index for index, value in enumerate(options) if value == st.session_state.get("selected_run_id")), 0)
    selected_id = st.selectbox(
        "RUN_ID",
        options,
        index=default_index,
        format_func=lambda value: next(
            f"{item['created_at_utc']} · {item['phase']} · {item['evidence_level']} · {item['data_origin']} · {value}"
            for item in runs
            if item["run_id"] == value
        ),
        key="history_run_id",
    )
    if st.button("Abrir relatório e proveniência", type="primary", key="open_run"):
        st.session_state["selected_run_id"] = selected_id
    try:
        run = load_run(root, st.session_state.get("selected_run_id", selected_id))
    except Exception as error:  # noqa: BLE001
        _show_error(root, "Falha ao abrir o run", error)
        return

    _render_run_summary(run)
    st.subheader("Relatório HTML e dados JSON")
    html, json_col = st.columns([2, 1])
    with html:
        if run.get("report_html"):
            with st.expander("Abrir prévia do relatório HTML", expanded=True):
                st.html(run["report_html"])
            st.download_button(
                "Baixar relatório HTML",
                data=run["report_html"].encode("utf-8"),
                file_name=f"{run['run_id']}-report.html",
                mime="text/html",
                width="stretch",
                on_click="ignore",
            )
        else:
            st.info("Este run não contém report.html.")
    with json_col:
        st.download_button(
            "Baixar run.json",
            data=run["run_json_text"].encode("utf-8"),
            file_name=f"{run['run_id']}-run.json",
            mime="application/json",
            width="stretch",
            on_click="ignore",
        )
        st.download_button(
            "Baixar metrics.json",
            data=run["metrics_json_text"].encode("utf-8"),
            file_name=f"{run['run_id']}-metrics.json",
            mime="application/json",
            width="stretch",
            on_click="ignore",
        )
    st.subheader("Proveniência e política")
    st.json({"run": run["manifest"], "sources": run["sources"]}, expanded=False)


def _snapshot_ml_defaults(snapshot: dict) -> tuple[str, str, str, str] | None:
    manifest = snapshot.get("manifest") or {}
    sources = manifest.get("sources") or []
    if isinstance(sources, dict):
        sources = list(sources.values())
    sources = [item for item in sources if item.get("timeframe") == "1m"]
    starts = [item.get("first_open_at_utc") for item in sources if item.get("first_open_at_utc")]
    ends = [item.get("last_open_at_utc") for item in sources if item.get("last_open_at_utc")]
    if not starts or not ends:
        return None
    start = min(datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC) for value in starts)
    end = max(datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC) for value in ends) + timedelta(minutes=1)
    midpoint = start + timedelta(seconds=int((end - start).total_seconds() * 0.60))
    midpoint = midpoint.replace(minute=0, second=0, microsecond=0)
    if midpoint <= start or midpoint >= end:
        return None
    stamp = lambda value: value.isoformat().replace("+00:00", "Z")
    return stamp(start), stamp(midpoint), stamp(midpoint), stamp(end)


def _render_ml_page(root: Path, catalog: dict) -> None:
    st.header("Experimento supervisionado F6")
    st.caption(
        "Classificação simples, um split temporal, labels futuros explícitos e comparação spot no motor F3. "
        "Snapshots selados como holdout não são elegíveis."
    )
    sealed = set(catalog.get("sealed_holdout_ids", []))
    eligible = [
        item for item in catalog.get("snapshots", [])
        if item.get("qa_status") == "PASS" and item["snapshot_id"] not in sealed
    ]
    if not eligible:
        st.info("Nenhum snapshot QA PASS disponível para ML. Crie um snapshot em Dados; holdouts selados não são elegíveis.")
    else:
        snapshot_id = st.selectbox(
            "Snapshot de desenvolvimento",
            [item["snapshot_id"] for item in eligible],
            format_func=lambda value: next(
                f"{item['symbol']} · {item['snapshot_id']} · {item['data_origin']}"
                for item in eligible if item["snapshot_id"] == value
            ),
            key="ml_snapshot_id",
        )
        selected = next(item for item in eligible if item["snapshot_id"] == snapshot_id)
        defaults = _snapshot_ml_defaults(selected)
        if defaults is None:
            st.warning("O manifesto deste snapshot não informa uma cobertura 1m utilizável para sugerir períodos.")
        train_start_default, train_end_default, validation_start_default, validation_end_default = defaults or ("", "", "", "")
        delay_default = int(selected.get("delay_assumed_ms", 1000))
        with st.form(f"ml_form_{snapshot_id}"):
            st.markdown("**Tarefa fixa:** retorno simples do close atual ao close de H barras futuras acima da barreira aproximada de fee + slippage round-trip.")
            first, second = st.columns(2)
            with first:
                timeframe = st.selectbox("Timeframe", ["1h", "4h"], key="ml_timeframe")
                horizon = int(st.number_input("Horizonte H (barras futuras)", min_value=1, max_value=240, value=6, step=1, key="ml_horizon"))
                train_start = st.text_input("Treino: início UTC inclusivo", value=train_start_default, key=f"ml_train_start_{snapshot_id}")
                train_end = st.text_input("Treino: fim UTC exclusivo", value=train_end_default, key=f"ml_train_end_{snapshot_id}")
                validation_start = st.text_input("Validação: início UTC inclusivo", value=validation_start_default, key=f"ml_validation_start_{snapshot_id}")
                validation_end = st.text_input("Validação: fim UTC exclusivo", value=validation_end_default, key=f"ml_validation_end_{snapshot_id}")
            with second:
                cash = st.number_input("Capital inicial (USDT)", min_value=1.0, value=1000.0, step=100.0, key="ml_cash")
                fee = st.number_input("Fee hipotética por lado (bps)", min_value=0.0, value=10.0, step=1.0, key="ml_fee")
                slippage = st.number_input("Slippage proxy por lado (bps)", min_value=0.0, value=5.0, step=1.0, key="ml_slippage")
                delay_ms = int(st.number_input("Atraso assumido (ms)", min_value=1, max_value=300000, value=max(delay_default, 1), step=100, key="ml_delay"))
                sma_fast = int(st.number_input("Baseline SMA rápida", min_value=1, max_value=499, value=20, step=1, key="ml_sma_fast"))
                sma_slow = int(st.number_input("Baseline SMA lenta", min_value=2, max_value=1000, value=50, step=1, key="ml_sma_slow"))
            st.caption("A primeira subjanela da validação calibra o threshold; só a subjanela cronológica posterior entra na avaliação econômica e nas métricas. Oito candidatos de threshold são registrados, sem otimização de modelo.")
            requested = st.form_submit_button("Executar experimento F6")
        if requested:
            try:
                with st.spinner("Treinando somente no intervalo definido e executando os modelos no simulador spot..."):
                    result = run_ml_experiment(
                        root,
                        snapshot_id,
                        horizon=horizon,
                        train_start_at_utc=train_start,
                        train_end_at_utc_exclusive=train_end,
                        validation_start_at_utc=validation_start,
                        validation_end_at_utc_exclusive=validation_end,
                        timeframe=timeframe,
                        initial_cash_usdt=Decimal(str(cash)),
                        fee_bps_per_side=Decimal(str(fee)),
                        slippage_bps_per_side=Decimal(str(slippage)),
                        assumed_data_delay_seconds=Decimal(delay_ms) / Decimal("1000"),
                        sma_fast=sma_fast,
                        sma_slow=sma_slow,
                    )
                st.success(
                    f"{result['manifest']['incremental_status']} · {result['manifest']['experiment_id']} · "
                    f"{result['manifest']['sample_counts']['training_examples']} amostras de treino · "
                    f"{result['manifest']['threshold_calibration']['trial_count']} trials registrados"
                )
                st.rerun()
            except Exception as error:  # noqa: BLE001
                _show_error(root, "Experimento F6 não concluído", error)

    ml_runs = list_ml_runs(root)
    if not ml_runs:
        return
    st.divider()
    st.subheader("Resultados F6 salvos")
    selected_id = st.selectbox(
        "Experimento salvo",
        [item["manifest"]["experiment_id"] for item in ml_runs],
        format_func=lambda value: next(
            f"{item['manifest']['created_at_utc']} · {item['manifest']['incremental_status']} · {value}"
            for item in ml_runs if item["manifest"]["experiment_id"] == value
        ),
        key="ml_saved_run",
    )
    saved = next(item for item in ml_runs if item["manifest"]["experiment_id"] == selected_id)
    manifest = saved["manifest"]
    directory = saved["directory"]
    st.write(f"Evidência `{manifest['evidence_level']}` · origem `{manifest['data_origin']}` · snapshot `{manifest['snapshot_id']}`")
    st.subheader(manifest["incremental_status"])
    st.dataframe(pd.DataFrame(manifest["economic_comparison"]), hide_index=True, width="stretch")
    st.markdown("**Treino e validação**")
    st.json({"fold": manifest["fold"], "sample_counts": manifest["sample_counts"], "fit": manifest["fit"]}, expanded=False)
    st.markdown("**Métricas probabilísticas na validação de avaliação**")
    st.json(manifest["predictive_metrics"], expanded=False)
    calibration_rows = []
    calibration_labels = {
        "ML_NULL": "Modelo nulo",
        "ML_LOGISTIC_PRICE": "Logística · preço",
        "ML_LOGISTIC_PRICE_VOLUME": "Logística · preço + volume",
    }
    for model, metrics in manifest["predictive_metrics"].items():
        for point in metrics["calibration_curve_5_bins"]:
            calibration_rows.append({
                "modelo": calibration_labels.get(model, model),
                "probabilidade média": point["mean_probability"],
                "frequência positiva": point["observed_positive_rate"],
            })
    if calibration_rows:
        calibration_figure = px.line(
            pd.DataFrame(calibration_rows),
            x="probabilidade média",
            y="frequência positiva",
            color="modelo",
            markers=True,
            title="Curva de calibração · validação posterior à calibração do threshold",
        )
        calibration_figure.add_scatter(
            x=[0, 1], y=[0, 1], mode="lines", name="Calibração ideal", line={"dash": "dash"}
        )
        calibration_figure.update_layout(xaxis_range=[0, 1], yaxis_range=[0, 1])
        st.plotly_chart(calibration_figure, key=f"ml_calibration_{selected_id}")
    left, right = st.columns(2)
    with left:
        st.download_button(
            "Baixar relatório HTML",
            data=(directory / "report.html").read_bytes(),
            file_name=f"{selected_id}-report.html",
            mime="text/html",
        )
    with right:
        st.download_button(
            "Exportar previsões e labels",
            data=(directory / "predictions.csv").read_bytes(),
            file_name=f"{selected_id}-predictions.csv",
            mime="text/csv",
        )
    predictions = pd.read_csv(directory / "predictions.csv")
    st.caption(f"O CSV mantém {len(predictions)} previsões com pred_at, feature_at/available_at, label_start/end/observable, features e papel do subperíodo.")
    st.dataframe(
        predictions[[
            "prediction_at_utc", "label_end_at_utc", "future_return_label",
            "logistic_price_probability", "logistic_price_threshold",
            "logistic_price_volume_probability", "logistic_price_volume_threshold", "period_role",
        ]].head(50),
        hide_index=True,
        width="stretch",
    )
    st.caption(f"Artefatos: {directory} · backtest canônico: {manifest['backtest_run_dir']}")


def _render_paper_replay_page(root: Path, catalog: dict) -> None:
    st.header("Paper Replay")
    st.info(
        "`PAPER_REPLAY` · avanço sequencial offline em candles históricos, "
        "sem feed incremental e sem ordens para exchange."
    )
    snapshots = [
        item for item in catalog.get("snapshots", [])
        if item.get("qa_status") == "PASS"
        and item.get("snapshot_id") not in catalog.get("sealed_holdout_ids", [])
    ]
    if not snapshots:
        st.warning("Não há snapshot QA PASS disponível fora de holdout selado. Crie um snapshot em Dados antes de iniciar.")
    else:
        with st.form("paper_replay_start_form"):
            snapshot_id = st.selectbox(
                "Snapshot imutável",
                [item["snapshot_id"] for item in snapshots],
                format_func=lambda value: next(
                    f"{value} · {item['data_origin']} · QA PASS"
                    for item in snapshots if item["snapshot_id"] == value
                ),
            )
            selected_snapshot = next(item for item in snapshots if item["snapshot_id"] == snapshot_id)
            manifest = selected_snapshot.get("manifest") or {}
            strategy = st.selectbox(
                "Estratégia a congelar nesta sessão",
                ["BUY_HOLD", "SMA_CROSS", "BREAKOUT", "CASH"],
                format_func=lambda value: STRATEGY_LABELS[value],
            )
            first, second = st.columns(2)
            with first:
                timeframe = st.selectbox("Timeframe de decisão", ["1h", "4h"], index=0)
                capital = st.number_input("Capital inicial (USDT)", min_value=1.0, value=1000.0, step=100.0)
                sma_fast = 20
                sma_slow = 50
                breakout_lookback = 20
                if strategy == "SMA_CROSS":
                    sma_fast = int(st.number_input("SMA rápida", min_value=1, max_value=500, value=20, step=1))
                    sma_slow = int(st.number_input("SMA lenta", min_value=2, max_value=1000, value=50, step=1))
                elif strategy == "BREAKOUT":
                    breakout_lookback = int(st.number_input("Lookback do breakout", min_value=1, max_value=1000, value=20, step=1))
            with second:
                fee_bps = st.number_input("Fee hipotética por lado (bps)", min_value=0.0, value=10.0, step=1.0)
                slippage_bps = st.number_input("Slippage proxy por lado (bps)", min_value=0.0, value=5.0, step=1.0)
                delay_ms = st.number_input(
                    "Atraso assumido (ms)", min_value=1, max_value=300000,
                    value=max(int(manifest.get("delay_assumed_ms", selected_snapshot.get("delay_assumed_ms", 1000))), 1),
                    step=100,
                    help="Hipótese de disponibilidade; não é latência observada.",
                )
                max_exposure = st.number_input(
                    "Exposição máxima (%)", min_value=1.0, max_value=100.0, value=100.0, step=5.0,
                )
                max_drawdown = st.number_input(
                    "Drawdown máximo da simulação (%)", min_value=0.1, max_value=100.0, value=30.0, step=1.0,
                )
            create_requested = st.form_submit_button("Congelar e iniciar replay", type="primary")
        st.caption(
            f"Fills usam F3: primeiro open 1m estritamente posterior a available_at, "
            f"fee {fee_bps:g} bps/lado e slippage proxy {slippage_bps:g} bps/lado. "
            f"Exposição até {max_exposure:g}% · drawdown limite {max_drawdown:g}%."
        )
        if create_requested:
            try:
                ensure_snapshot_available(root, snapshot_id)
                snapshot = load_backtest_snapshot(root, snapshot_id)
                config = BacktestConfig(
                    strategy=StrategyName(strategy),
                    decision_timeframe=timeframe,
                    initial_cash_usdt=Decimal(str(capital)),
                    fee_bps_per_side=Decimal(str(fee_bps)),
                    slippage_bps_per_side=Decimal(str(slippage_bps)),
                    assumed_data_delay_seconds=Decimal(int(delay_ms)) / Decimal("1000"),
                    sma_fast=sma_fast,
                    sma_slow=sma_slow,
                    breakout_lookback=breakout_lookback,
                )
                session = create_paper_replay(
                    root,
                    snapshot,
                    config,
                    RiskLimits(
                        max_exposure_fraction=Decimal(str(max_exposure)) / Decimal("100"),
                        max_drawdown_pct=Decimal(str(max_drawdown)),
                    ),
                )
                st.session_state["paper_replay_id"] = session.replay_id
                st.session_state["f7_verified_replay_id"] = session.replay_id
                st.success(f"Estratégia congelada. Replay `{session.replay_id}` iniciado.")
                st.rerun()
            except Exception as error:  # noqa: BLE001
                _show_error(root, "Replay não iniciado; nenhum run anterior foi alterado", error)

    sessions = list_paper_replays(root)
    if not sessions:
        st.info("Inicie um replay acima para acompanhar relógio, ledger e eventos persistidos.")
        return
    replay_ids = [item.replay_id for item in sessions]
    requested_id = st.session_state.get("paper_replay_id")
    if requested_id not in replay_ids:
        requested_id = replay_ids[0]
    replay_id = st.selectbox(
        "Sessão de replay",
        replay_ids,
        index=replay_ids.index(requested_id),
        format_func=lambda value: next(
            f"{item.replay_id} · {item.status.value} · {item.config.strategy.value}"
            for item in sessions if item.replay_id == value
        ),
        key="paper_replay_session_picker",
    )
    st.session_state["paper_replay_id"] = replay_id
    session = get_paper_replay(root, replay_id)
    try:
        snapshot = load_backtest_snapshot(root, session.snapshot_id)
    except Exception as error:  # noqa: BLE001
        _show_error(root, "Snapshot imutável do replay não pôde ser reaberto", error)
        return

    if st.session_state.get("f7_verified_replay_id") != replay_id:
        check = reconcile_paper_replay(root, replay_id, snapshot)
        st.session_state["f7_verified_replay_id"] = replay_id
        if not check["ok"]:
            st.error(f"RiskGate interrompeu o replay: {check['reason']}")
        else:
            st.caption(
                f"Retomada/reabertura reconciliada: {check['replayed_fills']} fills reexecutados "
                f"do journal e {check['equity_points']} pontos de equity conferidos."
            )
        session = get_paper_replay(root, replay_id)

    batch_size = int(st.number_input(
        "Candles 1m por atualização automática", min_value=1, max_value=1000, value=60, step=30,
        key="paper_replay_batch_size",
    ))
    controls = st.columns(4)
    if session.status is ReplayStatus.RUNNING and controls[0].button("Pausar", key=f"pause_{replay_id}"):
        pause_paper_replay(root, replay_id)
        st.rerun()
    if session.status is ReplayStatus.READY and controls[1].button("Retomar", key=f"resume_{replay_id}", type="primary"):
        resumed = resume_paper_replay(root, replay_id, snapshot)
        st.session_state["f7_verified_replay_id"] = resumed.replay_id
        st.rerun()
    if session.status in {ReplayStatus.RUNNING, ReplayStatus.READY} and controls[2].button("Parar", key=f"stop_{replay_id}"):
        stop_paper_replay(root, replay_id)
        st.rerun()
    reviewed_halt = st.checkbox(
        "Confirmo que revisei os motivos e quero criar uma sessão nova desde o início",
        key=f"review_reset_{replay_id}",
    )
    if controls[3].button(
        "Reset explícito",
        key=f"reset_{replay_id}",
        disabled=not reviewed_halt or session.status is ReplayStatus.RUNNING,
    ):
        try:
            new_session = reset_paper_replay(
                root, replay_id, snapshot, reviewed_halt=reviewed_halt,
            )
            st.session_state["paper_replay_id"] = new_session.replay_id
            st.session_state["f7_verified_replay_id"] = new_session.replay_id
            st.rerun()
        except Exception as error:  # noqa: BLE001
            _show_error(root, "Reset não realizado; a sessão anterior foi preservada", error)

    def render_replay_status() -> None:
        current = get_paper_replay(root, replay_id)
        if current.status is ReplayStatus.RUNNING:
            current = advance_paper_replay(
                root, replay_id, snapshot, max_candles=batch_size,
            )
            if current.status is not ReplayStatus.RUNNING:
                st.rerun(scope="app")
        st.markdown(f"**Estado:** `{current.status.value}` · **Evidência:** `PAPER_REPLAY`")
        st.caption(f"Motivo/transição atual: {current.state_reason} · origem: {current.data_origin}")
        st.caption(
            f"Snapshot: `{current.snapshot_id}` · SHA-256 `{current.snapshot_sha256}` · "
            f"configuração congelada SHA-256 `{current.config_sha256}`"
        )
        st.caption(
            f"Relógio simulado UTC: {_format_datetime(current.simulated_at_utc)} · "
            f"candle processado: {current.processed_index + 1} / {len(snapshot.candles_1m)}"
        )
        position_value = current.current_equity_usdt - current.cash_usdt
        first, second, third, fourth = st.columns(4)
        first.metric("Caixa", f"{current.cash_usdt:,.2f} USDT")
        second.metric("Posição marcada", f"{position_value:,.2f} USDT", f"{current.base_qty} base")
        third.metric("Equity", f"{current.current_equity_usdt:,.2f} USDT")
        fourth.metric("Drawdown máximo", f"{current.max_drawdown_pct:.2f}%")
        if current.status is ReplayStatus.HALTED:
            st.error(f"HALTED: {current.state_reason}. Nenhum novo fill será aceito nesta sessão.")
        elif current.status is ReplayStatus.DEGRADED:
            st.warning(f"DEGRADED: {current.state_reason}. Revise o fim dos dados antes de resetar.")
        events = list_paper_replay_events(root, replay_id, limit=40)
        st.subheader("Journal recente")
        st.dataframe(
            pd.DataFrame([{
                "seq": item["sequence"],
                "horário UTC": item["occurred_at_utc"],
                "evento": item["event_type"],
                "detalhes": json.dumps(item["payload"], ensure_ascii=False, sort_keys=True),
            } for item in events]),
            hide_index=True,
            width="stretch",
        )

    fragment = st.fragment(run_every=1.0 if session.status is ReplayStatus.RUNNING else None)(render_replay_status)
    fragment()

    session = get_paper_replay(root, replay_id)
    if session.processed_index >= 0 and session.status is not ReplayStatus.RUNNING:
        if st.button("Comparar com backtest F3 no mesmo snapshot e intervalo", key=f"compare_{replay_id}"):
            try:
                compare_paper_replay_to_backtest(root, replay_id, snapshot)
                st.rerun()
            except Exception as error:  # noqa: BLE001
                _show_error(root, "Comparação F3 não concluída", error)
    if session.comparison:
        comparison = session.comparison
        st.subheader("Replay vs backtest F3")
        st.write("Fills e pontos de equity iguais." if comparison.get("equal") else "Há diferenças; veja a lista abaixo.")
        if comparison.get("differences"):
            st.dataframe(pd.DataFrame(comparison["differences"]), hide_index=True, width="stretch")
        st.caption("Políticas adicionadas pelo F7:")
        for difference in comparison.get("policy_differences", []):
            st.write(f"- {difference}")
        with st.expander("Detalhes da comparação"):
            st.json(comparison)

    if session.status is not ReplayStatus.RUNNING:
        journal = export_paper_replay_journal(root, replay_id)
        col_a, col_b, col_c = st.columns(3)
        with col_a:
            st.download_button(
                "Exportar journal JSON",
                data=json.dumps(journal, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                file_name=f"{replay_id}-journal.json",
                mime="application/json",
                key=f"export_journal_{replay_id}",
            )
        with col_b:
            st.download_button(
                "Exportar equity CSV",
                data=export_equity_csv(root, replay_id),
                file_name=f"{replay_id}-equity.csv",
                mime="text/csv",
                key=f"export_equity_{replay_id}",
            )
        with col_c:
            st.download_button(
                "Exportar relatório HTML",
                data=export_paper_replay_html(root, replay_id),
                file_name=f"{replay_id}-report.html",
                mime="text/html",
                key=f"export_html_{replay_id}",
            )
        check = reconcile_paper_replay(root, replay_id, snapshot)
        st.caption(
            f"Conciliação do ledger: {'PASS' if check['ok'] else 'FAIL'} · "
            f"fills reexecutados: {check.get('replayed_fills', 0)} · "
            f"equity conferida: {check.get('equity_points', 0)} pontos · motivo: {check['reason']}"
        )
    else:
        st.caption("Pause ou encerre o replay para exportar o journal/equity do checkpoint atual.")


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return "ainda não iniciado"
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _render_paper_forward_page(root: Path) -> None:
    st.header("Paper Forward · Binance Spot BTCUSDT")
    st.caption("PAPER_FORWARD · feed WebSocket público recente · BUY & hold · ledger simulado F3 · sem ordens reais")
    st.warning(
        "A sessão espera uma vela de 1 minuto totalmente observada após a ativação. Fee e slippage são hipóteses; "
        "retenção/redistribuição do feed não teve licença verificada."
    )
    duration = st.number_input("Duração da sessão (segundos)", min_value=150, max_value=3600, value=180, step=30)
    st.caption("O relógio será sincronizado pelo endpoint público. A sessão para em feed/relógio inválido, gap, sequência quebrada ou drawdown limite.")
    launch_state = st.session_state.get("paper_forward_process")
    stop_path = root / "artifacts" / "paper_forward" / "stop.request"
    if launch_state:
        process = launch_state["process"]
        if process.poll() is None:
            st.info(f"Sessão {launch_state['run_id']} em execução (PID {process.pid}).")
            left, right = st.columns(2)
            if left.button("Atualizar status", key="paper_forward_refresh"):
                st.rerun()
            if right.button("Parar sessão", key="paper_forward_stop"):
                stop_path.parent.mkdir(parents=True, exist_ok=True)
                stop_path.write_text("stop\n", encoding="utf-8")
                st.info("Pedido de parada registrado; a sessão fechará o journal no próximo heartbeat.")
        else:
            log_path = Path(launch_state["log_path"])
            output = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else "Sem saída de processo."
            st.code(output[-12000:], language="text")
            session_path = root / "artifacts" / "paper_forward" / launch_state["run_id"]
            report_path = session_path / "report.html"
            journal_path = session_path / "journal.jsonl"
            if report_path.exists():
                st.success(f"Artefatos da sessão: {session_path}")
                st.download_button("Baixar relatório HTML", report_path.read_bytes(), file_name=f"{launch_state['run_id']}-report.html", mime="text/html")
            if journal_path.exists():
                st.download_button("Baixar journal JSONL", journal_path.read_bytes(), file_name=f"{launch_state['run_id']}-journal.jsonl", mime="application/x-ndjson")
            if st.button("Limpar status local", key="paper_forward_clear"):
                st.session_state.pop("paper_forward_process", None)
                st.rerun()
    elif st.button("Iniciar PAPER_FORWARD", type="primary", key="paper_forward_start"):
        run_id = f"PF-{uuid4().hex[:12]}"
        output_dir = root / "artifacts" / "paper_forward"
        output_dir.mkdir(parents=True, exist_ok=True)
        log_path = output_dir / f"{run_id}.launcher.log"
        command = [
            sys.executable, "-m", "quantlab", "paper-forward", "run",
            "--duration-seconds", str(int(duration)), "--run-id", run_id, "--root", str(root),
        ]
        with log_path.open("wb") as log_file:
            process = subprocess.Popen(
                command, cwd=root, stdout=log_file, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        st.session_state["paper_forward_process"] = {
            "run_id": run_id, "process": process, "log_path": str(log_path),
        }
        st.success(f"Iniciado {run_id}; use Atualizar status para acompanhar.")
        st.rerun()
    else:
        st.code("uv run quantlab paper-forward run --duration-seconds 180", language="powershell")
        st.caption("Alternativa de terminal: omita --duration-seconds para aguardar Ctrl+C ou pedido de parada.")


def main() -> None:
    st.set_page_config(page_title="QuantLab · laboratório local", page_icon="📈", layout="wide")
    root = _project_root()
    catalog = load_catalog_overview(root)
    runs = list_runs(root)
    st.title("QuantLab")
    st.caption("Laboratório local · Spot long-only · resultados exploratórios · sem ordens reais")
    page = st.sidebar.radio("Navegação", PAGE_NAMES, key="active_page")
    st.sidebar.caption(f"Projeto: {root}")
    st.sidebar.caption("Atraso, fee e slippage são hipóteses. Dados históricos são IN_SAMPLE.")

    if page == "Dados":
        _render_data_page(root, catalog, runs)
    elif page == "Backtests":
        _render_backtest_page(root, catalog, runs)
    elif page == "Operações":
        _render_operations_page(root, runs)
    elif page == "Comparar":
        _render_comparison_page(root, runs)
    elif page == "Pesquisa":
        _render_research_page(root, catalog)
    elif page == "ML":
        _render_ml_page(root, catalog)
    elif page == "Paper Replay":
        _render_paper_replay_page(root, catalog)
    elif page == "Paper Forward":
        _render_paper_forward_page(root)
    else:
        _render_history_page(root, runs)


if __name__ == "__main__":
    main()
