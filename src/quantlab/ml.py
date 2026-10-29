"""F6: pequeno experimento de classificacao temporal, com evidencias auditaveis."""

from __future__ import annotations

import csv
import html
import json
import math
import statistics
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from quantlab.backtest import (
    BacktestConfig,
    BacktestSnapshot,
    _aggregate_decision_candles,
    run_backtest_window,
)
from quantlab.backtest_artifacts import write_backtest_artifacts
from quantlab.data_quality import load_backtest_snapshot
from quantlab.domain import StrategyName, TargetExposure
from quantlab.ui.storage import load_catalog_overview


PRICE_FEATURES = (
    "return_1",
    "return_3",
    "return_6",
    "volatility_12",
    "range_12",
    "distance_sma_20",
)
VOLUME_FEATURE = "relative_volume_20"
THRESHOLD_GRID = (0.40, 0.50, 0.60, 0.70)
TRIAL_BUDGET = 10
MIN_TRAIN_SAMPLES = 40
MIN_VALIDATION_SAMPLES = 30
MIN_CALIBRATION_SAMPLES = 8
ROUND_TRIP_COST_APPROXIMATION = (
    "2 * (fee_bps_per_side + slippage_bps_per_side) / 10,000; barreira aproximada, "
    "separada dos custos realmente debitados pelo simulador"
)


class MLExperimentError(ValueError):
    """Entrada ou janela insuficiente para uma experiencia F6 reproduzivel."""


@dataclass(frozen=True, slots=True)
class LabeledSample:
    sample_id: str
    decision_open_at_utc: datetime
    feature_at_utc: datetime
    available_at_utc: datetime
    label_start_at_utc: datetime
    label_end_at_utc: datetime
    label_observable_at_utc: datetime
    future_return: float
    label: int
    features: Mapping[str, float]


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _utc(value: str | datetime, field: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as error:
            raise MLExperimentError(f"{field} deve ser ISO-8601 com timezone UTC.") from error
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise MLExperimentError(f"{field} deve conter timezone UTC explícito.")
    return value.astimezone(UTC)


def build_labeled_samples(
    snapshot: BacktestSnapshot,
    *,
    timeframe: str,
    horizon: int,
    assumed_data_delay_seconds: Decimal,
    fee_bps_per_side: Decimal,
    slippage_bps_per_side: Decimal,
) -> tuple[LabeledSample, ...]:
    """Build trailing-only features and future-close labels on complete segments."""
    if not isinstance(horizon, int) or isinstance(horizon, bool) or horizon < 1:
        raise MLExperimentError("H deve ser pelo menos 1 barra de decisão.")
    if timeframe not in {"1h", "4h"}:
        raise MLExperimentError("Timeframe de decisão deve ser 1h ou 4h.")
    if not assumed_data_delay_seconds.is_finite() or assumed_data_delay_seconds <= 0:
        raise MLExperimentError("O atraso assumido deve ser positivo.")
    if (
        not fee_bps_per_side.is_finite()
        or not slippage_bps_per_side.is_finite()
        or fee_bps_per_side < 0
        or slippage_bps_per_side < 0
        or fee_bps_per_side >= 10000
        or slippage_bps_per_side >= 10000
    ):
        raise MLExperimentError("Fee e slippage devem ser finitos, não negativos e menores que 10.000 bps por lado.")
    delay_microseconds = assumed_data_delay_seconds * Decimal("1000000")
    if delay_microseconds != delay_microseconds.to_integral_value():
        raise MLExperimentError("O atraso deve ter precisão máxima de microssegundos.")

    decisions, _ = _aggregate_decision_candles(snapshot.candles_1m, timeframe)
    if not decisions:
        raise MLExperimentError(f"Snapshot sem barras completas de {timeframe}.")
    delay = timedelta(microseconds=int(assumed_data_delay_seconds * Decimal("1000000")))
    round_trip_hurdle = float(
        Decimal("2") * (fee_bps_per_side + slippage_bps_per_side) / Decimal("10000")
    )

    segments: list[tuple[int, int]] = []
    segment_start = 0
    for index in range(1, len(decisions)):
        if decisions[index].open_at_utc != decisions[index - 1].end_at_utc:
            segments.append((segment_start, index - 1))
            segment_start = index
    segments.append((segment_start, len(decisions) - 1))

    samples: list[LabeledSample] = []
    for first, last in segments:
        for index in range(first + 19, last - horizon + 1):
            current = decisions[index]
            prior = decisions[index - 1]
            future = decisions[index + horizon]
            returns_1 = [
                float(decisions[position].close / decisions[position - 1].close) - 1.0
                for position in range(index - 11, index + 1)
            ]
            trailing_ranges = [
                float((decisions[position].high - decisions[position].low) / decisions[position].close)
                for position in range(index - 11, index + 1)
            ]
            sma_20 = sum(
                (decisions[position].close for position in range(index - 19, index + 1)),
                start=Decimal("0"),
            ) / Decimal("20")
            volume_mean_20 = sum(
                (decisions[position].volume_base for position in range(index - 19, index + 1)),
                start=Decimal("0"),
            ) / Decimal("20")
            relative_volume = (
                float(current.volume_base / volume_mean_20) if volume_mean_20 > 0 else 0.0
            )
            features = {
                "return_1": float(current.close / prior.close - 1),
                "return_3": float(current.close / decisions[index - 3].close - 1),
                "return_6": float(current.close / decisions[index - 6].close - 1),
                "volatility_12": statistics.pstdev(returns_1),
                "range_12": statistics.fmean(trailing_ranges),
                "distance_sma_20": float(current.close / sma_20 - 1),
                VOLUME_FEATURE: relative_volume,
            }
            if not all(math.isfinite(value) for value in features.values()):
                continue

            future_return = float(future.close / current.close - 1)
            label = int(future_return > round_trip_hurdle)
            feature_at = current.end_at_utc
            available_at = feature_at + delay
            label_end = future.end_at_utc
            samples.append(
                LabeledSample(
                    sample_id=f"{timeframe}:{_iso(feature_at)}:H{horizon}",
                    decision_open_at_utc=current.open_at_utc,
                    feature_at_utc=feature_at,
                    available_at_utc=available_at,
                    label_start_at_utc=feature_at,
                    label_end_at_utc=label_end,
                    label_observable_at_utc=label_end + delay,
                    future_return=future_return,
                    label=label,
                    features=features,
                )
            )
    return tuple(samples)


def fit_predict_probabilities(
    train_samples: Sequence[LabeledSample],
    prediction_samples: Sequence[LabeledSample],
) -> dict[str, tuple[float, ...]]:
    """Fit two train-only logistic pipelines and return a train-prior null control."""
    if len(train_samples) < MIN_TRAIN_SAMPLES:
        raise MLExperimentError(
            f"Treino tem {len(train_samples)} exemplos após purge; mínimo operacional: {MIN_TRAIN_SAMPLES}."
        )
    if not prediction_samples:
        raise MLExperimentError("Não há previsões temporais para o intervalo escolhido.")
    train_labels = [sample.label for sample in train_samples]
    if set(train_labels) != {0, 1}:
        raise MLExperimentError("O treino precisa conter labels positivos e negativos para a regressão logística.")

    predictions: dict[str, tuple[float, ...]] = {}
    predictions[StrategyName.ML_NULL.value] = tuple(
        sum(train_labels) / len(train_labels) for _ in prediction_samples
    )
    feature_sets = {
        StrategyName.ML_LOGISTIC_PRICE.value: PRICE_FEATURES,
        StrategyName.ML_LOGISTIC_PRICE_VOLUME.value: (*PRICE_FEATURES, VOLUME_FEATURE),
    }
    for model_name, feature_names in feature_sets.items():
        pipeline = Pipeline(
            steps=[
                ("imputer", SimpleImputer(strategy="median")),
                ("scaler", StandardScaler()),
                (
                    "model",
                    LogisticRegression(
                        C=1.0,
                        solver="liblinear",
                        max_iter=1000,
                        random_state=0,
                    ),
                ),
            ]
        )
        train_x = [
            [sample.features[name] for name in feature_names]
            for sample in train_samples
        ]
        prediction_x = [
            [sample.features[name] for name in feature_names]
            for sample in prediction_samples
        ]
        pipeline.fit(train_x, train_labels)
        fitted_model = pipeline.named_steps["model"]
        positive_column = list(fitted_model.classes_).index(1)
        predictions[model_name] = tuple(
            float(row[positive_column]) for row in pipeline.predict_proba(prediction_x)
        )
    return predictions


def _balanced_accuracy(labels: Sequence[int], probabilities: Sequence[float], threshold: float) -> tuple[float, str]:
    predicted = [int(probability >= threshold) for probability in probabilities]
    positives = sum(label == 1 for label in labels)
    negatives = len(labels) - positives
    if positives == 0 or negatives == 0:
        accuracy = sum(actual == guessed for actual, guessed in zip(labels, predicted, strict=True)) / len(labels)
        return accuracy, "accuracy_single_class_calibration"
    true_positive = sum(actual == 1 and guessed == 1 for actual, guessed in zip(labels, predicted, strict=True))
    true_negative = sum(actual == 0 and guessed == 0 for actual, guessed in zip(labels, predicted, strict=True))
    return (true_positive / positives + true_negative / negatives) / 2, "balanced_accuracy"


def _calibrate_thresholds(
    calibration_labels: Sequence[int],
    model_probabilities: Mapping[str, Sequence[float]],
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    selected: dict[str, float] = {}
    trials: list[dict[str, Any]] = []
    trial_index = 0
    for model_name in (
        StrategyName.ML_LOGISTIC_PRICE.value,
        StrategyName.ML_LOGISTIC_PRICE_VOLUME.value,
    ):
        candidates: list[dict[str, Any]] = []
        probabilities = model_probabilities[model_name]
        for threshold in THRESHOLD_GRID:
            trial_index += 1
            score, metric = _balanced_accuracy(calibration_labels, probabilities, threshold)
            row = {
                "trial_index": trial_index,
                "model": model_name,
                "threshold": threshold,
                "calibration_metric": metric,
                "calibration_score": score,
                "calibration_examples": len(calibration_labels),
                "selected": False,
            }
            candidates.append(row)
        winner = max(
            candidates,
            key=lambda row: (
                row["calibration_score"],
                -abs(row["threshold"] - 0.5),
                row["threshold"],
            ),
        )
        winner["selected"] = True
        selected[model_name] = float(winner["threshold"])
        trials.extend(candidates)
    return selected, trials


def _probability_metrics(labels: Sequence[int], probabilities: Sequence[float]) -> dict[str, Any]:
    if len(labels) != len(probabilities) or not labels:
        raise MLExperimentError("Não foi possível calcular métricas probabilísticas sem labels de avaliação.")
    clipped = [min(max(probability, 1e-15), 1 - 1e-15) for probability in probabilities]
    brier = sum((probability - label) ** 2 for label, probability in zip(labels, probabilities, strict=True)) / len(labels)
    log_loss = -sum(
        label * math.log(probability) + (1 - label) * math.log(1 - probability)
        for label, probability in zip(labels, clipped, strict=True)
    ) / len(labels)
    bins: list[dict[str, float | int]] = []
    for bin_index in range(5):
        lower = bin_index / 5
        upper = (bin_index + 1) / 5
        members = [
            position for position, probability in enumerate(probabilities)
            if lower <= probability < upper or (bin_index == 4 and probability == 1.0)
        ]
        if not members:
            continue
        bins.append({
            "lower_probability": lower,
            "upper_probability": upper,
            "mean_probability": statistics.fmean(probabilities[position] for position in members),
            "observed_positive_rate": statistics.fmean(labels[position] for position in members),
            "examples": len(members),
        })
    return {
        "examples": len(labels),
        "brier_score": brier,
        "log_loss": log_loss,
        "calibration_curve_5_bins": bins,
    }


def _calibration_svg(metrics: Mapping[str, Mapping[str, Any]]) -> str:
    width, height = 600, 380
    left, top, plot_width, plot_height = 64, 40, 480, 250
    bottom = top + plot_height
    colors = {
        StrategyName.ML_NULL.value: "#64748b",
        StrategyName.ML_LOGISTIC_PRICE.value: "#2563eb",
        StrategyName.ML_LOGISTIC_PRICE_VOLUME.value: "#d97706",
    }
    labels = {
        StrategyName.ML_NULL.value: "Modelo nulo",
        StrategyName.ML_LOGISTIC_PRICE.value: "Logística · preço",
        StrategyName.ML_LOGISTIC_PRICE_VOLUME.value: "Logística · preço + volume",
    }
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="Curvas de calibração: probabilidade prevista versus frequência positiva observada">',
        '<rect width="100%" height="100%" fill="#fff"/>',
        f'<text x="{left}" y="22" font-family="system-ui,sans-serif" font-size="14" fill="#17212b">Probabilidade prevista × frequência positiva observada</text>',
    ]
    for tick in (0, 0.25, 0.5, 0.75, 1):
        x = left + tick * plot_width
        y = bottom - tick * plot_height
        parts.extend([
            f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{bottom}" stroke="#e2e8f0"/>',
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_width}" y2="{y:.1f}" stroke="#e2e8f0"/>',
            f'<text x="{x:.1f}" y="{bottom + 18}" text-anchor="middle" font-family="system-ui,sans-serif" font-size="11" fill="#475569">{tick:.2f}</text>',
            f'<text x="{left - 9}" y="{y + 4:.1f}" text-anchor="end" font-family="system-ui,sans-serif" font-size="11" fill="#475569">{tick:.2f}</text>',
        ])
    parts.extend([
        f'<line x1="{left}" y1="{bottom}" x2="{left + plot_width}" y2="{top}" stroke="#94a3b8" stroke-dasharray="5 4"/>',
        f'<line x1="{left}" y1="{bottom}" x2="{left + plot_width}" y2="{bottom}" stroke="#475569"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" stroke="#475569"/>',
        f'<text x="{left + plot_width / 2}" y="{height - 8}" text-anchor="middle" font-family="system-ui,sans-serif" font-size="12" fill="#17212b">Probabilidade média por faixa</text>',
        f'<text x="15" y="{top + plot_height / 2}" transform="rotate(-90 15 {top + plot_height / 2})" text-anchor="middle" font-family="system-ui,sans-serif" font-size="12" fill="#17212b">Frequência positiva observada</text>',
    ])
    for model_name, model_metrics in metrics.items():
        bins = model_metrics["calibration_curve_5_bins"]
        points = sorted(
            (float(item["mean_probability"]), float(item["observed_positive_rate"]))
            for item in bins
        )
        color = colors.get(model_name, "#334155")
        label = html.escape(labels.get(model_name, model_name))
        if points:
            point_string = " ".join(
                f"{left + probability * plot_width:.1f},{bottom - observed * plot_height:.1f}"
                for probability, observed in points
            )
            if len(points) > 1:
                parts.append(f'<polyline points="{point_string}" fill="none" stroke="{color}" stroke-width="2"/>')
            for probability, observed in points:
                parts.append(
                    f'<circle cx="{left + probability * plot_width:.1f}" cy="{bottom - observed * plot_height:.1f}" r="4" fill="{color}"/>'
                )
        legend_y = height - 38
        index = list(metrics).index(model_name)
        legend_x = left + index * 174
        parts.extend([
            f'<line x1="{legend_x}" y1="{legend_y}" x2="{legend_x + 18}" y2="{legend_y}" stroke="{color}" stroke-width="3"/>',
            f'<text x="{legend_x + 24}" y="{legend_y + 4}" font-family="system-ui,sans-serif" font-size="11" fill="#17212b">{label}</text>',
        ])
    parts.append("</svg>")
    return "".join(parts)


def _strategy_metrics(result: Any) -> dict[str, str | int | bool]:
    metrics = result.metrics
    display_names = {
        StrategyName.ML_NULL: "Modelo nulo",
        StrategyName.ML_LOGISTIC_PRICE: "Logística · preço",
        StrategyName.ML_LOGISTIC_PRICE_VOLUME: "Logística · preço + volume",
        StrategyName.SMA_CROSS: "SMA",
        StrategyName.BUY_HOLD: "Buy & hold",
        StrategyName.CASH: "Cash",
    }
    return {
        "strategy": result.strategy.value,
        "strategy_label": display_names[result.strategy],
        "initial_equity_usdt": str(metrics.initial_equity_usdt),
        "final_equity_usdt": str(metrics.final_equity_usdt),
        "gross_pnl_usdt": str(metrics.gross_pnl_usdt),
        "total_fees_usdt": str(metrics.total_fees_usdt),
        "total_slippage_cost_usdt": str(metrics.total_slippage_cost_usdt),
        "net_pnl_usdt": str(metrics.net_pnl_usdt),
        "net_return_pct": str(metrics.net_return_pct),
        "max_drawdown_pct": str(metrics.max_drawdown_pct),
        "exposure_pct": str(metrics.exposure_pct),
        "turnover_quote_usdt": str(metrics.turnover_quote_usdt),
        "buy_count": metrics.buy_count,
        "sell_count": metrics.sell_count,
        "veto_count": metrics.veto_count,
        "open_position": metrics.open_position,
    }


def _write_predictions(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    feature_names = (*PRICE_FEATURES, VOLUME_FEATURE)
    fields = [
        "sample_id",
        "prediction_at_utc",
        "feature_at_utc",
        "feature_available_at_utc",
        "label_start_at_utc",
        "label_end_at_utc",
        "label_observable_at_utc",
        "future_return_label",
        "future_return",
        "logistic_price_probability",
        "logistic_price_threshold",
        "logistic_price_target",
        "logistic_price_volume_probability",
        "logistic_price_volume_threshold",
        "logistic_price_volume_target",
        "null_model_probability",
        "null_model_threshold",
        "null_model_target",
        "period_role",
        *feature_names,
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _report_html(manifest: Mapping[str, Any]) -> str:
    comparison_rows = []
    for metrics in manifest["economic_comparison"]:
        comparison_rows.append(
            "<tr>"
            f"<td>{html.escape(str(metrics['strategy_label']))}</td>"
            f"<td>{html.escape(str(metrics['net_return_pct']))}%</td>"
            f"<td>{html.escape(str(metrics['final_equity_usdt']))} USDT</td>"
            f"<td>{html.escape(str(metrics['total_fees_usdt']))} USDT</td>"
            f"<td>{html.escape(str(metrics['total_slippage_cost_usdt']))} USDT</td>"
            f"<td>{html.escape(str(metrics['max_drawdown_pct']))}%</td>"
            f"<td>{html.escape(str(metrics['exposure_pct']))}%</td>"
            "</tr>"
        )
    trial_rows = []
    for trial in manifest["threshold_trials"]:
        trial_rows.append(
            "<tr>"
            f"<td>{trial['trial_index']}</td><td>{html.escape(trial['model'])}</td>"
            f"<td>{trial['threshold']:.2f}</td><td>{html.escape(trial['calibration_metric'])}</td>"
            f"<td>{trial['calibration_score']:.4f}</td>"
            f"<td>{'selecionado' if trial['selected'] else 'avaliado'}</td></tr>"
        )
    prediction_rows = []
    for prediction in manifest["prediction_examples"]:
        prediction_rows.append(
            "<tr>"
            f"<td>{html.escape(prediction['prediction_at_utc'])}</td>"
            f"<td>{html.escape(prediction['label_end_at_utc'])}</td>"
            f"<td>{prediction['future_return_label']}</td>"
            f"<td>{prediction['logistic_price_probability']:.4f}</td>"
            f"<td>{prediction['logistic_price_volume_probability']:.4f}</td>"
            f"<td>{html.escape(prediction['period_role'])}</td></tr>"
        )
    task = manifest["task"]
    fold = manifest["fold"]
    counts = manifest["sample_counts"]
    return f"""<!doctype html>
<html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>QuantLab F6 · {html.escape(manifest['experiment_id'])}</title>
<style>body{{font:15px/1.55 system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#17212b}}table{{border-collapse:collapse;width:100%;margin:1rem 0 2rem}}th,td{{border-bottom:1px solid #d8e0e8;padding:.5rem;text-align:left}}th{{background:#f3f6f8}}.status{{font-weight:700;font-size:1.2rem;padding:.8rem;background:#f3f6f8}}code{{overflow-wrap:anywhere}}</style>
<body><h1>Experimento supervisionado F6</h1>
<p class="status">{html.escape(manifest['incremental_status'])}</p>
<p>Dados: <strong>{html.escape(manifest['data_origin'])}</strong> · evidência: <strong>{html.escape(manifest['evidence_level'])}</strong> · snapshot: <code>{html.escape(manifest['snapshot_id'])}</code></p>
<h2>Tarefa e relógio do label</h2>
<p>{html.escape(task['description'])} Horizonte H={task['horizon']} barras de {html.escape(task['timeframe'])}. O recurso usa dados fechados até <code>feature_at</code>; sua disponibilidade/predição é <code>feature_at + delay</code>. Label começa em <code>label_start_at</code>, termina em <code>label_end_at</code> e só é observável em <code>label_observable_at</code>.</p>
<p>Barreira: {html.escape(task['round_trip_cost_rule'])}. É uma aproximação da tarefa de classificação; os custos efetivos continuam sendo debitados separadamente pelo simulador.</p>
<h2>Fold e purge</h2>
<ul><li>Treino: {html.escape(fold['train_start_at_utc'])} até {html.escape(fold['train_end_at_utc_exclusive'])}</li>
<li>Validação total: {html.escape(fold['validation_start_at_utc'])} até {html.escape(fold['validation_end_at_utc_exclusive'])}</li>
<li>Calibração do threshold: {html.escape(fold['calibration_start_at_utc'])} até antes da primeira previsão avaliada em {html.escape(fold['first_evaluation_prediction_at_utc'])}</li>
<li>Avaliação financeira e métricas preditivas: {html.escape(fold['evaluation_start_at_utc'])} até {html.escape(fold['validation_end_at_utc_exclusive'])}</li>
<li>Primeira previsão usada nas métricas: {html.escape(fold['first_evaluation_prediction_at_utc'])}</li>
<li>Amostras de treino: {counts['training_examples']}; candidatas antes do purge: {counts['training_candidates']}; removidas por label ainda não observável no início da validação: {counts['purged_training_labels']}; validação com label observável: {counts['validation_examples']}; calibração utilizável após purge: {counts['threshold_calibration_examples']}; avaliação: {counts['validation_evaluation_examples']}.</li></ul>
<p>Maior <code>label_observable_at</code> do treino: <code>{html.escape(fold['max_training_label_observable_at_utc'])}</code>, estritamente anterior ao início da validação: <strong>{fold['training_labels_observable_before_validation']}</strong>.</p>
<p>Imputer, scaler e LogisticRegression foram ajustados somente no treino. Nenhum label de validação entra no fit; labels da primeira parte da validação calibram o threshold e não entram na avaliação financeira posterior. Holdout selado não foi consultado.</p>
<h2>Trials de threshold (máximo 10; este experimento registrou {manifest['trial_count']})</h2>
<p>Modelo e hiperparâmetros ficam fixos; os {manifest['trial_count']} candidatos do threshold foram definidos no código antes da avaliação e todos estão registrados. O controle nulo usa a prevalência do treino e threshold fixo 0,50.</p>
<table><thead><tr><th>#</th><th>Modelo</th><th>Threshold</th><th>Métrica na calibração</th><th>Score</th><th>Estado</th></tr></thead><tbody>{''.join(trial_rows)}</tbody></table>
<h2>Métricas preditivas na avaliação</h2>
<p>Probabilidades: Brier, log loss e curva de calibração em cinco faixas. São métricas da janela de validação indicada; não significam lucro esperado.</p>
{_calibration_svg(manifest['predictive_metrics'])}
<pre>{html.escape(json.dumps(manifest['predictive_metrics'], indent=2, ensure_ascii=False))}</pre>
<h2>Comparação econômica no mesmo motor F3</h2>
<p>SMA, controle nulo e dois modelos foram simulados no mesmo instrumento, intervalo, capital, fee, slippage e política de fill. Fill continua no primeiro open 1m estritamente posterior a <code>available_at</code>. O retorno líquido inclui fee e slippage da engine spot long-only.</p>
<table><thead><tr><th>Estratégia</th><th>Retorno líquido</th><th>Equity final</th><th>Fees</th><th>Slippage</th><th>Drawdown máx.</th><th>Exposição</th></tr></thead><tbody>{''.join(comparison_rows)}</tbody></table>
<h2>Previsões para inspeção</h2>
<p>Amostra das primeiras previsões out-of-sample por tempo. O CSV anexo contém todas as previsões, features, <code>pred_at</code> e <code>label_end</code>.</p>
<table><thead><tr><th>pred_at UTC</th><th>label_end UTC</th><th>Label</th><th>Logística preço</th><th>Preço + volume</th><th>Subperíodo</th></tr></thead><tbody>{''.join(prediction_rows)}</tbody></table>
<h2>Limites da evidência</h2>
<ul>{''.join(f'<li>{html.escape(item)}</li>' for item in manifest['limitations'])}</ul>
<p>Este resultado não demonstra rentabilidade futura. Acurácia ou probabilidade não é sinônimo de lucro; a comparação usa apenas um fold e um período de validação.</p>
</body></html>"""


def _prepare_split(
    samples: Sequence[LabeledSample],
    *,
    train_start: datetime,
    train_end: datetime,
    validation_start: datetime,
    validation_end: datetime,
) -> tuple[list[LabeledSample], list[LabeledSample], int]:
    if not train_start < train_end <= validation_start < validation_end:
        raise MLExperimentError("Use janelas crescentes: train_start < train_end <= validation_start < validation_end.")
    train_candidates = [
        sample for sample in samples
        if train_start <= sample.available_at_utc < train_end
    ]
    train = [
        sample for sample in train_candidates
        if sample.label_observable_at_utc < validation_start
    ]
    validation = [
        sample for sample in samples
        if validation_start <= sample.available_at_utc < validation_end
        and sample.label_observable_at_utc < validation_end
    ]
    if len(train) < MIN_TRAIN_SAMPLES:
        raise MLExperimentError(
            f"Treino disponível após purge: {len(train)} exemplos; aumente a janela de treino antes da validação."
        )
    if len(validation) < MIN_VALIDATION_SAMPLES:
        raise MLExperimentError(
            f"Validação com label observável: {len(validation)} exemplos; mínimo para separar calibração e avaliação: {MIN_VALIDATION_SAMPLES}."
        )
    return train, validation, len(train_candidates) - len(train)


def _prediction_rows(
    validation_samples: Sequence[LabeledSample],
    probabilities: Mapping[str, Sequence[float]],
    thresholds: Mapping[str, float],
    evaluation_start: datetime,
) -> list[dict[str, Any]]:
    null_threshold = 0.5
    rows: list[dict[str, Any]] = []
    for index, sample in enumerate(validation_samples):
        price_probability = probabilities[StrategyName.ML_LOGISTIC_PRICE.value][index]
        volume_probability = probabilities[StrategyName.ML_LOGISTIC_PRICE_VOLUME.value][index]
        null_probability = probabilities[StrategyName.ML_NULL.value][index]
        rows.append({
            "sample_id": sample.sample_id,
            "prediction_at_utc": _iso(sample.available_at_utc),
            "feature_at_utc": _iso(sample.feature_at_utc),
            "feature_available_at_utc": _iso(sample.available_at_utc),
            "label_start_at_utc": _iso(sample.label_start_at_utc),
            "label_end_at_utc": _iso(sample.label_end_at_utc),
            "label_observable_at_utc": _iso(sample.label_observable_at_utc),
            "future_return_label": sample.label,
            "future_return": sample.future_return,
            "logistic_price_probability": price_probability,
            "logistic_price_threshold": thresholds[StrategyName.ML_LOGISTIC_PRICE.value],
            "logistic_price_target": int(price_probability >= thresholds[StrategyName.ML_LOGISTIC_PRICE.value]),
            "logistic_price_volume_probability": volume_probability,
            "logistic_price_volume_threshold": thresholds[StrategyName.ML_LOGISTIC_PRICE_VOLUME.value],
            "logistic_price_volume_target": int(volume_probability >= thresholds[StrategyName.ML_LOGISTIC_PRICE_VOLUME.value]),
            "null_model_probability": null_probability,
            "null_model_threshold": null_threshold,
            "null_model_target": int(null_probability >= null_threshold),
            "period_role": "validation_evaluation" if sample.available_at_utc >= evaluation_start else "threshold_calibration",
            **sample.features,
        })
    return rows


def _atomic_json(path: Path, value: object) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    temp_path.replace(path)


def run_ml_experiment_on_snapshot(
    root: Path,
    snapshot: BacktestSnapshot,
    *,
    horizon: int,
    train_start_at_utc: str | datetime,
    train_end_at_utc_exclusive: str | datetime,
    validation_start_at_utc: str | datetime,
    validation_end_at_utc_exclusive: str | datetime,
    timeframe: str = "1h",
    initial_cash_usdt: Decimal = Decimal("1000"),
    fee_bps_per_side: Decimal = Decimal("10"),
    slippage_bps_per_side: Decimal = Decimal("5"),
    assumed_data_delay_seconds: Decimal = Decimal("1"),
    sma_fast: int = 20,
    sma_slow: int = 50,
) -> dict[str, Any]:
    """Run the one-fold F6 experiment and publish inspectable, local artifacts."""
    train_start = _utc(train_start_at_utc, "train_start_at_utc")
    train_end = _utc(train_end_at_utc_exclusive, "train_end_at_utc_exclusive")
    validation_start = _utc(validation_start_at_utc, "validation_start_at_utc")
    validation_end = _utc(validation_end_at_utc_exclusive, "validation_end_at_utc_exclusive")
    if not sma_fast < sma_slow:
        raise MLExperimentError("A baseline SMA exige fast < slow.")

    catalog = load_catalog_overview(root)
    if catalog.get("catalog_error"):
        raise MLExperimentError("Não foi possível verificar os snapshots selados; operação F6 bloqueada com segurança.")
    if snapshot.snapshot_id in set(catalog.get("sealed_holdout_ids", [])):
        raise MLExperimentError("Snapshot SELADO como holdout F5; F6 não pode consultá-lo.")

    delay = Decimal(str(assumed_data_delay_seconds))
    samples = build_labeled_samples(
        snapshot,
        timeframe=timeframe,
        horizon=horizon,
        assumed_data_delay_seconds=delay,
        fee_bps_per_side=fee_bps_per_side,
        slippage_bps_per_side=slippage_bps_per_side,
    )
    train_samples, validation_samples, purged_count = _prepare_split(
        samples,
        train_start=train_start,
        train_end=train_end,
        validation_start=validation_start,
        validation_end=validation_end,
    )
    probabilities = fit_predict_probabilities(train_samples, validation_samples)

    calibration_count = max(10, len(validation_samples) // 3)
    if len(validation_samples) - calibration_count < 10:
        raise MLExperimentError("A validação é curta demais para separar calibração e avaliação.")
    proposed_calibration = validation_samples[:calibration_count]
    evaluation_samples = validation_samples[calibration_count:]
    evaluation_start = evaluation_samples[0].available_at_utc
    calibration_samples = [
        sample for sample in proposed_calibration
        if sample.label_observable_at_utc < evaluation_start
    ]
    if len(calibration_samples) < MIN_CALIBRATION_SAMPLES:
        raise MLExperimentError(
            f"Só há {len(calibration_samples)} labels de calibração observáveis antes da avaliação; "
            "aumente o período de validação ou reduza H."
        )
    validation_indices = {sample.sample_id: index for index, sample in enumerate(validation_samples)}
    calibration_indices = [validation_indices[sample.sample_id] for sample in calibration_samples]
    evaluation_indices = [validation_indices[sample.sample_id] for sample in evaluation_samples]
    thresholds, threshold_trials = _calibrate_thresholds(
        [sample.label for sample in calibration_samples],
        {
            model_name: [values[index] for index in calibration_indices]
            for model_name, values in probabilities.items()
            if model_name != StrategyName.ML_NULL.value
        },
    )
    if len(threshold_trials) > TRIAL_BUDGET:
        raise MLExperimentError("Orçamento interno de trials excedido.")

    evaluation_labels = [validation_samples[index].label for index in evaluation_indices]
    predictive_metrics = {
        model_name: _probability_metrics(
            evaluation_labels,
            [values[index] for index in evaluation_indices],
        )
        for model_name, values in probabilities.items()
    }
    prediction_rows = _prediction_rows(
        validation_samples,
        probabilities,
        thresholds,
        evaluation_start,
    )
    economic_rows = [
        row for row in prediction_rows
        if row["period_role"] == "validation_evaluation"
    ]
    targets = {
        StrategyName.ML_NULL: {
            sample.feature_at_utc: TargetExposure(Decimal(str(row["null_model_target"])))
            for sample, row in zip(evaluation_samples, economic_rows, strict=True)
        },
        StrategyName.ML_LOGISTIC_PRICE: {
            sample.feature_at_utc: TargetExposure(Decimal(str(row["logistic_price_target"])))
            for sample, row in zip(evaluation_samples, economic_rows, strict=True)
        },
        StrategyName.ML_LOGISTIC_PRICE_VOLUME: {
            sample.feature_at_utc: TargetExposure(Decimal(str(row["logistic_price_volume_target"])))
            for sample, row in zip(evaluation_samples, economic_rows, strict=True)
        },
    }
    config = BacktestConfig(
        strategy=StrategyName.SMA_CROSS,
        decision_timeframe=timeframe,
        initial_cash_usdt=initial_cash_usdt,
        fee_bps_per_side=fee_bps_per_side,
        slippage_bps_per_side=slippage_bps_per_side,
        assumed_data_delay_seconds=delay,
        sma_fast=sma_fast,
        sma_slow=sma_slow,
    )
    evaluation_start_open = evaluation_samples[0].decision_open_at_utc
    backtest = run_backtest_window(
        snapshot,
        config,
        start_at_utc=evaluation_start_open,
        end_at_utc=validation_end,
        evidence_level="DEMO" if snapshot.data_origin == "SYNTHETIC" else "WALK_FORWARD",
        target_overrides=targets,
    )
    backtest_results = {item.strategy: item for item in backtest.results}
    comparison = [
        _strategy_metrics(backtest_results[strategy])
        for strategy in (
            StrategyName.ML_NULL,
            StrategyName.ML_LOGISTIC_PRICE,
            StrategyName.ML_LOGISTIC_PRICE_VOLUME,
            StrategyName.SMA_CROSS,
            StrategyName.BUY_HOLD,
            StrategyName.CASH,
        )
    ]
    control_return = max(
        float(backtest_results[StrategyName.ML_NULL].metrics.net_return_pct),
        float(backtest_results[StrategyName.SMA_CROSS].metrics.net_return_pct),
    )
    linear_returns = [
        float(backtest_results[strategy].metrics.net_return_pct)
        for strategy in (StrategyName.ML_LOGISTIC_PRICE, StrategyName.ML_LOGISTIC_PRICE_VOLUME)
    ]
    no_incremental_gain = all(value <= control_return for value in linear_returns)
    incremental_status = (
        "SEM GANHO INCREMENTAL"
        if no_incremental_gain
        else "GANHO INCREMENTAL OBSERVADO NA VALIDAÇÃO (exploratório; não confirmado independentemente)"
    )

    experiment_id = f"f6-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    max_training_observable = max(sample.label_observable_at_utc for sample in train_samples)
    fold = {
        "fold_id": "f6-temporal-single-split-v1",
        "train_start_at_utc": _iso(train_start),
        "train_end_at_utc_exclusive": _iso(train_end),
        "validation_start_at_utc": _iso(validation_start),
        "validation_end_at_utc_exclusive": _iso(validation_end),
        "calibration_start_at_utc": _iso(validation_samples[0].available_at_utc),
        "calibration_end_at_utc_exclusive": _iso(evaluation_start),
        "evaluation_start_at_utc": _iso(evaluation_start_open),
        "first_evaluation_prediction_at_utc": _iso(evaluation_start),
        "max_training_label_observable_at_utc": _iso(max_training_observable),
        "training_labels_observable_before_validation": max_training_observable < validation_start,
        "gap_or_purge_rule": "treino mantém somente labels com label_observable_at < validation_start",
    }
    task = {
        "name": "classificacao_binaria_retorno_futuro_acima_da_barreira_de_custos",
        "description": (
            "classificação de retorno simples entre o close da barra de decisão atual e o close de H barras futuras "
            "acima de uma barreira aproximada de custos spot round-trip"
        ),
        "horizon": horizon,
        "timeframe": timeframe,
        "label_start_definition": "end_at da barra de decisão atual",
        "label_end_definition": "end_at da barra de decisão H barras à frente, dentro do mesmo segmento sem gaps",
        "label_observable_definition": "label_end_at + assumed_data_delay_seconds",
        "round_trip_cost_rule": ROUND_TRIP_COST_APPROXIMATION,
        "round_trip_cost_hurdle_fraction": str(
            Decimal("2") * (fee_bps_per_side + slippage_bps_per_side) / Decimal("10000")
        ),
    }
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "phase": "F6_ML",
        "experiment_id": experiment_id,
        "created_at_utc": _iso(datetime.now(UTC)),
        "status": "COMPLETE",
        "incremental_status": incremental_status,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_sha256": snapshot.content_sha256,
        "instrument_id": snapshot.instrument_id,
        "data_origin": snapshot.data_origin,
        "evidence_level": "DEMO" if snapshot.data_origin == "SYNTHETIC" else "WALK_FORWARD",
        "task": task,
        "fold": fold,
        "sample_counts": {
            "training_candidates": len(train_samples) + purged_count,
            "training_examples": len(train_samples),
            "purged_training_labels": purged_count,
            "validation_examples": len(validation_samples),
            "threshold_calibration_proposed": len(proposed_calibration),
            "threshold_calibration_examples": len(calibration_samples),
            "validation_evaluation_examples": len(evaluation_samples),
        },
        "training_label_max_observable_at_utc": _iso(max_training_observable),
        "feature_sets": {
            "price_only": list(PRICE_FEATURES),
            "price_plus_volume": [*PRICE_FEATURES, VOLUME_FEATURE],
            "construction": "janelas exclusivamente à direita e dentro do segmento contínuo; sem center=True, normalização global ou ffill",
            "timestamp": "feature_at=close da barra; available_at=feature_at+delay; pred_at=available_at",
        },
        "fit": {
            "pipeline": ["SimpleImputer(median)", "StandardScaler", "LogisticRegression"],
            "fit_sample_count": len(train_samples),
            "fit_timestamp_max": _iso(max(sample.available_at_utc for sample in train_samples)),
            "fit_label_observable_max": _iso(max_training_observable),
            "model_hyperparameters": {"C": 1.0, "solver": "liblinear", "max_iter": 1000, "random_state": 0},
            "fit_only_train": True,
        },
        "threshold_calibration": {
            "validation_subperiod_only": True,
            "method": "balanced accuracy na primeira subjanela de validação; desempate pela distância de 0,50 e depois threshold maior",
            "single_class_fallback": "usa accuracy se a subjanela de calibração contiver somente uma classe; cada trial registra qual métrica foi aplicada",
            "thresholds_by_model": thresholds,
            "null_threshold_fixed": 0.5,
            "trials": threshold_trials,
            "trial_count": len(threshold_trials),
            "trial_budget": TRIAL_BUDGET,
        },
        "threshold_trials": threshold_trials,
        "trial_count": len(threshold_trials),
        "predictive_metrics": predictive_metrics,
        "economic_comparison": comparison,
        "validation_policy": {
            "holdout_accessed": False,
            "validation_labels_used_for_fit": False,
            "calibration_labels_used_only_for_threshold": True,
            "evaluation_labels_used_only_for_metrics_after_prediction": True,
            "prediction_to_target": "probability >= threshold -> TargetExposure(1); otherwise TargetExposure(0)",
            "target_to_fill": "MESMA run_backtest_window/_simulate_strategy F3; primeiro open 1m estritamente elegível; custos e ledger compartilhados",
            "comparison_period_start_at_utc": _iso(evaluation_start_open),
            "comparison_period_end_at_utc_exclusive": _iso(validation_end),
            "incremental_control": "modelo linear precisa superar simultaneamente ML_NULL e SMA_CROSS em retorno líquido para não receber SEM GANHO INCREMENTAL",
        },
        "backtest_config": {
            "decision_timeframe": timeframe,
            "initial_cash_usdt": str(initial_cash_usdt),
            "fee_bps_per_side": str(fee_bps_per_side),
            "slippage_bps_per_side": str(slippage_bps_per_side),
            "assumed_data_delay_seconds": str(delay),
            "sma_fast": sma_fast,
            "sma_slow": sma_slow,
        },
        "limitations": [
            "um único split temporal, um snapshot e um instrumento; evidência exploratória, sem independência estatística demonstrada",
            "threshold usa labels da primeira parte da validação; resultados econômicos são apenas da parte posterior da mesma validação",
            "seleção entre dois modelos lineares e comparação na mesma janela reduzem a independência da avaliação",
            "label usa close futuro e barreira aproximada; custos financeiros reais seguem o fill model F3, que pode divergir do label",
            "available_at é atraso assumido; fill OPEN 1m é proxy OHLCV, sem spread observado, fila, impacto ou latência real",
            "não há holdout aberto, paper trading, ordens ou evidência de rentabilidade futura",
            "dados sintéticos só demonstram o circuito de software; dados reais continuam IN_SAMPLE/WALK_FORWARD e não provam edge",
        ],
    }

    audit = dict(backtest.audit)
    audit["phase"] = "F6_ML"
    audit["ml_context"] = {
        "experiment_id": experiment_id,
        "task": task,
        "fold": fold,
        "sample_counts": manifest["sample_counts"],
        "trial_count": len(threshold_trials),
        "trial_budget": TRIAL_BUDGET,
        "thresholds_by_model": thresholds,
        "incremental_status": incremental_status,
        "prediction_artifact": f"artifacts/ml/{experiment_id}/predictions.csv",
    }
    backtest = replace(backtest, audit=audit)
    backtest_artifacts = write_backtest_artifacts(root / "artifacts" / "backtests", backtest)
    manifest["backtest_run_id"] = backtest_artifacts["run_id"]
    manifest["backtest_run_dir"] = backtest_artifacts["directory"].relative_to(root).as_posix()

    manifest["prediction_examples"] = [
        {
            key: row[key]
            for key in (
                "prediction_at_utc",
                "label_end_at_utc",
                "future_return_label",
                "logistic_price_probability",
                "logistic_price_volume_probability",
                "period_role",
            )
        }
        for row in prediction_rows[:30]
    ]
    report_html = _report_html(manifest)
    output_dir = root / "artifacts" / "ml" / experiment_id
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = output_dir.parent / f".{experiment_id}.tmp"
    staging_dir.mkdir()
    try:
        _atomic_json(staging_dir / "experiment.json", manifest)
        _atomic_json(staging_dir / "threshold_trials.json", threshold_trials)
        _write_predictions(staging_dir / "predictions.csv", prediction_rows)
        (staging_dir / "report.html").write_text(report_html, encoding="utf-8")
        staging_dir.replace(output_dir)
    except Exception:
        if staging_dir.exists():
            for child in staging_dir.iterdir():
                child.unlink()
            staging_dir.rmdir()
        raise

    paths = {
        "directory": output_dir,
        "experiment_json": output_dir / "experiment.json",
        "threshold_trials_json": output_dir / "threshold_trials.json",
        "predictions_csv": output_dir / "predictions.csv",
        "report_html": output_dir / "report.html",
        "backtest_run_dir": backtest_artifacts["directory"],
        "backtest_run_id": backtest_artifacts["run_id"],
    }
    return {"manifest": manifest, "paths": paths}


def run_ml_experiment(
    root: Path,
    snapshot_id: str,
    **options: Any,
) -> dict[str, Any]:
    """Load one explicitly named, non-holdout QA snapshot and run F6."""
    catalog = load_catalog_overview(root)
    if catalog.get("catalog_error"):
        raise MLExperimentError("Não foi possível verificar os snapshots selados; operação F6 bloqueada com segurança.")
    if snapshot_id in set(catalog.get("sealed_holdout_ids", [])):
        raise MLExperimentError("Snapshot SELADO como holdout F5; F6 não pode consultá-lo.")
    snapshot = load_backtest_snapshot(root, snapshot_id)
    if "assumed_data_delay_seconds" not in options:
        delay_ms = (snapshot.source_metadata or {}).get("delay_assumed_ms", "1000")
        options["assumed_data_delay_seconds"] = Decimal(str(delay_ms)) / Decimal("1000")
    return run_ml_experiment_on_snapshot(root, snapshot, **options)


def list_ml_runs(root: Path) -> list[dict[str, Any]]:
    """Return finalized local F6 manifests, ignoring staging folders and symlinks."""
    output = root / "artifacts" / "ml"
    if not output.is_dir():
        return []
    runs: list[dict[str, Any]] = []
    for directory in output.iterdir():
        manifest_path = directory / "experiment.json"
        if directory.is_symlink() or not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get("experiment_id") != directory.name or manifest.get("phase") != "F6_ML":
            continue
        runs.append({"manifest": manifest, "directory": directory.resolve()})
    return sorted(runs, key=lambda item: item["manifest"].get("created_at_utc", ""), reverse=True)
