from __future__ import annotations

import hashlib
import gc
import os
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from quantlab.backtest import BacktestConfig, BacktestSnapshot
from quantlab.domain import Candle, StrategyName
from quantlab.paper_replay import (
    ReplayStatus,
    advance_paper_replay,
    compare_paper_replay_to_backtest,
    create_paper_replay,
    export_equity_csv,
    export_paper_replay_html,
    export_paper_replay_journal,
    get_paper_replay,
    pause_paper_replay,
    reconcile_paper_replay,
    resume_paper_replay,
)


def _snapshot() -> BacktestSnapshot:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    candles = tuple(
        Candle(
            open_at_utc=start + timedelta(minutes=index),
            end_at_utc=start + timedelta(minutes=index + 1),
            open=Decimal("100") + Decimal(index) / Decimal("100"),
            high=Decimal("100.1") + Decimal(index) / Decimal("100"),
            low=Decimal("99.9") + Decimal(index) / Decimal("100"),
            close=Decimal("100") + Decimal(index) / Decimal("100"),
            volume_base=Decimal("1"),
            instrument_id="BINANCE:SPOT:BTCUSDT",
        )
        for index in range(180)
    )
    content = "\n".join(
        "|".join(str(value) for value in (
            item.open_at_utc.isoformat(), item.open, item.high, item.low, item.close, item.volume_base,
        ))
        for item in candles
    ).encode()
    digest = hashlib.sha256(content).hexdigest()
    return BacktestSnapshot(
        snapshot_id="synthetic-f7-smoke-v1",
        instrument_id="BINANCE:SPOT:BTCUSDT",
        candles_1m=candles,
        content_sha256=digest,
        qa_status="PASS",
        data_origin="SYNTHETIC",
    )


def _gap_snapshot() -> BacktestSnapshot:
    complete = _snapshot()
    candles = complete.candles_1m[:10] + complete.candles_1m[11:]
    content = "\n".join(
        "|".join(str(value) for value in (
            item.open_at_utc.isoformat(), item.open, item.high, item.low, item.close, item.volume_base,
        ))
        for item in candles
    ).encode()
    return BacktestSnapshot(
        snapshot_id="synthetic-f7-gap-smoke-v1",
        instrument_id=complete.instrument_id,
        candles_1m=candles,
        content_sha256=hashlib.sha256(content).hexdigest(),
        qa_status="PASS",
        data_origin="SYNTHETIC",
    )


def _seed_ui_snapshot(root: Path, snapshot: BacktestSnapshot) -> None:
    database = root / "state" / "quantlab.sqlite"
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """CREATE TABLE dataset_snapshots (
               snapshot_id TEXT PRIMARY KEY,symbol TEXT,data_origin TEXT,qa_status TEXT,
               delay_assumed_ms INTEGER,content_sha256 TEXT,source_partition_count INTEGER,
               manifest_path TEXT,qa_json_path TEXT,qa_html_path TEXT,created_at_utc TEXT)"""
        )
        connection.execute(
            "INSERT INTO dataset_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                snapshot.snapshot_id, "BTCUSDT", "SYNTHETIC", "PASS", 1000,
                snapshot.content_sha256, 1, "missing-manifest.json", "", "", "2026-01-01T00:00:00Z",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def main() -> int:
    snapshot = _snapshot()
    config = BacktestConfig(
        strategy=StrategyName.BUY_HOLD,
        initial_cash_usdt=Decimal("1000"),
        fee_bps_per_side=Decimal("10"),
        slippage_bps_per_side=Decimal("5"),
    )
    with tempfile.TemporaryDirectory(prefix="quantlab-f7-smoke-") as temporary:
        root = Path(temporary)
        session = create_paper_replay(root, snapshot, config)
        advance_paper_replay(root, session.replay_id, snapshot, max_candles=3)
        pause_paper_replay(root, session.replay_id)

        # A fresh API call models reopening the app: the journal is replayed
        # into a temporary ledger and reconciled against every equity point.
        restarted = get_paper_replay(root, session.replay_id)
        assert restarted.status is ReplayStatus.READY
        assert reconcile_paper_replay(root, session.replay_id, snapshot)["replayed_fills"] == 1
        resumed = resume_paper_replay(root, session.replay_id, snapshot)
        assert resumed.status is ReplayStatus.RUNNING
        advance_paper_replay(root, session.replay_id, snapshot, max_candles=5)
        pause_paper_replay(root, session.replay_id)

        reconciliation = reconcile_paper_replay(root, session.replay_id, snapshot)
        assert reconciliation["ok"], reconciliation
        assert reconciliation["replayed_fills"] == 1
        comparison = compare_paper_replay_to_backtest(root, session.replay_id, snapshot)
        assert comparison["equal"], comparison["differences"]
        journal = export_paper_replay_journal(root, session.replay_id)
        assert journal["evidence_level"] == "PAPER_REPLAY"
        assert len(journal["fills"]) == 1
        assert len(journal["equity"]) == 8
        assert len(export_equity_csv(root, session.replay_id).splitlines()) == 9
        report = export_paper_replay_html(root, session.replay_id)
        assert "PAPER_REPLAY" in report
        assert "PAPER_FORWARD" not in report
        assert "Journal de eventos" in report
        project = Path(__file__).resolve().parents[1]
        ui_root = root / "ui"
        _seed_ui_snapshot(ui_root, snapshot)
        original_cwd = Path.cwd()
        try:
            os.chdir(ui_root)
            with patch("quantlab.data_quality.load_backtest_snapshot", return_value=snapshot):
                app = AppTest.from_file(
                    str(project / "src" / "quantlab" / "ui" / "app.py"),
                    default_timeout=30,
                )
                app.run()
                app.sidebar.radio[0].set_value("Paper Replay")
                app.run()
                if not any(button.label == "Congelar e iniciar replay" for button in app.button):
                    raise AssertionError("formulário de início do Paper Replay não apareceu")
                next(button for button in app.button if button.label == "Congelar e iniciar replay").click().run()
                if app.exception:
                    raise AssertionError([item.message for item in app.exception])
                ui_replay_id = app.session_state["paper_replay_id"]
                if get_paper_replay(ui_root, ui_replay_id).status is not ReplayStatus.RUNNING:
                    raise AssertionError("a UI não iniciou o replay")
                if not any(button.label == "Pausar" for button in app.button):
                    raise AssertionError("botão Pausar não apareceu durante RUNNING")
                next(button for button in app.button if button.label == "Pausar").click().run()
                if get_paper_replay(ui_root, ui_replay_id).status is not ReplayStatus.READY:
                    raise AssertionError("o botão Pausar não persistiu READY")
                if not any(button.label == "Retomar" for button in app.button):
                    raise AssertionError("botão Retomar não apareceu em READY")
                next(button for button in app.button if button.label == "Retomar").click().run()
                if app.exception:
                    raise AssertionError([item.message for item in app.exception])
                if get_paper_replay(ui_root, ui_replay_id).status is not ReplayStatus.RUNNING:
                    raise AssertionError("o botão Retomar não persistiu RUNNING")
                ui_journal = export_paper_replay_journal(ui_root, ui_replay_id)
                if len(ui_journal["fills"]) != 1:
                    raise AssertionError("pause/resume pela UI duplicou ou perdeu o fill")
                pause_paper_replay(ui_root, ui_replay_id)
                app.run()  # disable the RUNNING fragment before TemporaryDirectory cleanup
                if app.exception:
                    raise AssertionError([item.message for item in app.exception])
                del app
                gc.collect()
        finally:
            os.chdir(original_cwd)

        gap_snapshot = _gap_snapshot()
        gap_ui_root = root / "gap-ui"
        _seed_ui_snapshot(gap_ui_root, gap_snapshot)
        try:
            os.chdir(gap_ui_root)
            with patch("quantlab.data_quality.load_backtest_snapshot", return_value=gap_snapshot):
                gap_app = AppTest.from_file(
                    str(project / "src" / "quantlab" / "ui" / "app.py"),
                    default_timeout=30,
                )
                gap_app.run()
                gap_app.sidebar.radio[0].set_value("Paper Replay")
                gap_app.run()
                next(button for button in gap_app.button if button.label == "Congelar e iniciar replay").click().run()
                if gap_app.exception:
                    raise AssertionError([item.message for item in gap_app.exception])
                gap_replay_id = gap_app.session_state["paper_replay_id"]
                gap_replay = get_paper_replay(gap_ui_root, gap_replay_id)
                if gap_replay.status is not ReplayStatus.HALTED or gap_replay.state_reason != "GAP_DETECTED":
                    raise AssertionError(
                        f"UI não parou no gap sintético: {gap_replay.status} {gap_replay.state_reason}"
                    )
                if not any("HALTED: GAP_DETECTED" in item.value for item in gap_app.error):
                    raise AssertionError("a página não exibiu o motivo do estado HALTED")
                gap_journal = export_paper_replay_journal(gap_ui_root, gap_replay_id)
                if not any(
                    item["event_type"] == "RISK_VETO" and item["payload"].get("reason") == "GAP_DETECTED"
                    for item in gap_journal["events"]
                ):
                    raise AssertionError("journal da UI não registrou a causa do HALTED")
                del gap_app
                gc.collect()
        finally:
            os.chdir(original_cwd)
        print(
            "F7_SMOKE_OK",
            f"status={get_paper_replay(root, session.replay_id).status.value}",
            f"fills={len(journal['fills'])}",
            f"fees={journal['replay']['fees_paid_usdt']}",
            f"equity={journal['replay']['current_equity_usdt']}",
            f"comparison_equal={comparison['equal']}",
            "ui=start-pause-resume-gap-halt",
            "origin=SYNTHETIC",
            "evidence=PAPER_REPLAY",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
