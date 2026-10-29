"""F2: normalize Binance Spot 1m archives into audited Parquet snapshots.

Raw ZIPs and their manifests remain immutable. Every operation names raw object
IDs and snapshot IDs explicitly; this module never resolves a mutable "latest".
"""

from __future__ import annotations

import csv
import hashlib
import html
import json
import os
import re
import sqlite3
import tempfile
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Sequence
from uuid import uuid4

import duckdb

from quantlab.data_ingestion import _connect_catalog
from quantlab.domain import Candle

PARSER_VERSION = "quantlab-binance-spot-kline12-v1"
CURATED_SCHEMA_VERSION = "quantlab-spot-curated-v1"
QA_REPORT_VERSION = 2
DEFAULT_DELAY_MS = 1_000
MINUTE = timedelta(minutes=1)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
EXPECTED_COLUMNS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_asset_volume",
    "number_of_trades",
    "taker_buy_base_asset_volume",
    "taker_buy_quote_asset_volume",
    "ignore",
)
PRICE_FIELDS = ("open", "high", "low", "close")
DECIMAL_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "volume_base",
    "quote_volume",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
    "ignore_value",
)
PARQUET_COLUMNS = (
    "open_at_utc",
    "end_at_utc",
    "available_at_utc",
    "source_close_time_raw",
    "source_close_at_utc",
    "timestamp_unit",
    "open",
    "high",
    "low",
    "close",
    "volume_base",
    "quote_volume",
    "trade_count",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
    "ignore_value",
    "quality_status",
    "raw_object_id",
)
PARQUET_TYPES = {
    # UTC is explicit in the schema/column names. Plain DuckDB TIMESTAMP avoids
    # its Python TIMESTAMPTZ converter's optional pytz dependency.
    "open_at_utc": "TIMESTAMP",
    "end_at_utc": "TIMESTAMP",
    "available_at_utc": "TIMESTAMP",
    "source_close_time_raw": "BIGINT",
    "source_close_at_utc": "TIMESTAMP",
    "timestamp_unit": "VARCHAR",
    **{name: "DECIMAL(38,18)" for name in DECIMAL_FIELDS},
    "trade_count": "BIGINT",
    "quality_status": "VARCHAR",
    "raw_object_id": "VARCHAR",
}
_HEADER_NORMALIZER = re.compile(r"[^a-z0-9]+")


class DataQualityError(ValueError):
    """Erro F2 com código curto para relatório e quarentena."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Kline:
    open_at: datetime
    end_at: datetime
    available_at: datetime
    source_close_time_raw: int
    source_close_at: datetime
    timestamp_unit: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume_base: Decimal
    quote_volume: Decimal
    trade_count: int
    taker_buy_base_volume: Decimal
    taker_buy_quote_volume: Decimal
    ignore_value: Decimal
    raw_object_id: str

    def canonical_values(self) -> tuple[Any, ...]:
        return (
            self.open_at,
            self.end_at,
            self.available_at,
            self.source_close_time_raw,
            self.source_close_at,
            self.timestamp_unit,
            self.open,
            self.high,
            self.low,
            self.close,
            self.volume_base,
            self.quote_volume,
            self.trade_count,
            self.taker_buy_base_volume,
            self.taker_buy_quote_volume,
            self.ignore_value,
            "PASS",
            self.raw_object_id,
        )


def epoch_value_to_utc(value: int, unit: str) -> datetime:
    """Convert an explicitly declared epoch unit without float rounding."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise DataQualityError("TIMESTAMP_TYPE", "Timestamp deve ser inteiro.")
    if unit == "ms":
        return EPOCH + timedelta(milliseconds=value)
    if unit == "us":
        return EPOCH + timedelta(microseconds=value)
    raise DataQualityError("TIMESTAMP_UNIT", f"Unidade temporal não suportada: {unit!r}.")


def declared_timestamp_unit(year: int, month: int) -> str:
    """Binance Spot monthly klines: ms before 2025-01; us from 2025-01."""
    return "ms" if (year, month) < (2025, 1) else "us"


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _duck_timestamp_text(value: datetime) -> str:
    """Return ISO UTC wall-time for DuckDB TIMESTAMP columns."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "")


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=UTC)
    end = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1, tzinfo=UTC)
    return start, end


def _month_minutes(year: int, month: int) -> int:
    start, end = _month_bounds(year, month)
    return int((end - start).total_seconds() // 60)


def _missing_minute_ranges(bars: Sequence[Kline], start: datetime, end: datetime) -> list[dict[str, Any]]:
    observed = {bar.open_at for bar in bars}
    ranges: list[dict[str, Any]] = []
    range_start: datetime | None = None
    missing_count = 0
    current = start
    while current < end:
        if current not in observed:
            if range_start is None:
                range_start = current
            missing_count += 1
        elif range_start is not None:
            ranges.append({
                "start_at_utc": _utc_text(range_start),
                "end_at_utc_exclusive": _utc_text(current),
                "missing_minutes": missing_count,
            })
            range_start = None
            missing_count = 0
        current += MINUTE
    if range_start is not None:
        ranges.append({
            "start_at_utc": _utc_text(range_start),
            "end_at_utc_exclusive": _utc_text(end),
            "missing_minutes": missing_count,
        })
    return ranges


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _header_key(value: str) -> str:
    return _HEADER_NORMALIZER.sub("", value.lstrip("\ufeff").strip().lower())


def _is_native_header(row: Sequence[str]) -> bool:
    expected = tuple(_header_key(cell) for cell in EXPECTED_COLUMNS)
    return len(row) == 12 and tuple(_header_key(cell) for cell in row) == expected


def _parse_decimal(value: str, field: str, row_number: int) -> Decimal:
    try:
        number = Decimal(value.strip())
    except (InvalidOperation, AttributeError) as error:
        raise DataQualityError("SCHEMA_INVALID", f"linha {row_number}: {field} não é decimal válido.") from error
    if not number.is_finite():
        raise DataQualityError("SCHEMA_INVALID", f"linha {row_number}: {field} não é finito.")
    scale = max(0, -number.as_tuple().exponent)
    digits = len(number.as_tuple().digits)
    integer_digits = max(0, digits - scale)
    if scale > 18 or digits > 38 or integer_digits > 20:
        raise DataQualityError("DECIMAL_PRECISION", f"linha {row_number}: {field} excede DECIMAL(38,18).")
    return number


def _parse_int(value: str, field: str, row_number: int) -> int:
    try:
        if not value.strip() or any(character in value for character in ".eE"):
            raise ValueError
        return int(value.strip())
    except (ValueError, AttributeError) as error:
        raise DataQualityError("SCHEMA_INVALID", f"linha {row_number}: {field} deve ser inteiro.") from error


def _parse_row(
    row: Sequence[str], *, row_number: int, raw: sqlite3.Row, unit: str, delay_ms: int
) -> Kline:
    if len(row) != 12:
        raise DataQualityError("SCHEMA_INVALID", f"linha {row_number}: esperadas 12 colunas, recebidas {len(row)}.")
    open_raw = _parse_int(row[0], "open_time", row_number)
    close_raw = _parse_int(row[6], "close_time", row_number)
    open_at = epoch_value_to_utc(open_raw, unit)
    close_at = epoch_value_to_utc(close_raw, unit)
    period_start = datetime.fromisoformat(raw["period_start_utc"].replace("Z", "+00:00"))
    period_end = datetime.fromisoformat(raw["period_end_utc"].replace("Z", "+00:00"))
    if not (period_start <= open_at < period_end):
        raise DataQualityError(
            "TIMESTAMP_OUTSIDE_MONTH",
            f"linha {row_number}: {open_at.isoformat()} fora do mês solicitado {raw['year']:04d}-{raw['month']:02d} com unidade {unit}.",
        )
    if open_at.second or open_at.microsecond:
        raise DataQualityError("TIMESTAMP_ALIGNMENT", f"linha {row_number}: open_time não está alinhado ao minuto UTC.")
    end_at = open_at + MINUTE
    precision = timedelta(milliseconds=1) if unit == "ms" else timedelta(microseconds=1)
    if close_at != end_at - precision:
        raise DataQualityError(
            "SOURCE_CLOSE_TIME",
            f"linha {row_number}: close_time nativo não termina no limite esperado da vela ({unit}).",
        )

    values = {name: _parse_decimal(row[index], name, row_number) for index, name in enumerate(EXPECTED_COLUMNS) if name in {
        "open", "high", "low", "close", "volume", "quote_asset_volume",
        "taker_buy_base_asset_volume", "taker_buy_quote_asset_volume", "ignore",
    }}
    open_price, high, low, close = (values[name] for name in PRICE_FIELDS)
    if min(open_price, high, low, close) <= 0:
        raise DataQualityError("OHLC_INVALID", f"linha {row_number}: preço OHLC deve ser positivo.")
    if high < max(open_price, close, low) or low > min(open_price, close, high):
        raise DataQualityError("OHLC_INVALID", f"linha {row_number}: relação OHLC incoerente.")
    volume = values["volume"]
    quote_volume = values["quote_asset_volume"]
    trade_count = _parse_int(row[8], "number_of_trades", row_number)
    taker_base = values["taker_buy_base_asset_volume"]
    taker_quote = values["taker_buy_quote_asset_volume"]
    ignore = values["ignore"]
    if min(volume, quote_volume, taker_base, taker_quote, ignore) < 0 or trade_count < 0:
        raise DataQualityError("VOLUME_INVALID", f"linha {row_number}: volumes e trade_count não podem ser negativos.")
    return Kline(
        open_at=open_at,
        end_at=end_at,
        available_at=end_at + timedelta(milliseconds=delay_ms),
        source_close_time_raw=close_raw,
        source_close_at=close_at,
        timestamp_unit=unit,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume_base=volume,
        quote_volume=quote_volume,
        trade_count=trade_count,
        taker_buy_base_volume=taker_base,
        taker_buy_quote_volume=taker_quote,
        ignore_value=ignore,
        raw_object_id=raw["object_id"],
    )


def _parse_archive(path: Path, raw: sqlite3.Row, delay_ms: int) -> dict[str, Any]:
    unit = declared_timestamp_unit(raw["year"], raw["month"])
    expected_csv = raw["original_zip_name"][:-4] + ".csv"
    bars: dict[datetime, Kline] = {}
    errors: list[dict[str, Any]] = []
    duplicate_count = 0
    out_of_order = 0
    invalid_rows = 0
    source_rows = 0
    header_present = False
    last_open: datetime | None = None
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = archive.namelist()
            if names != [expected_csv]:
                raise DataQualityError("SCHEMA_INVALID", "ZIP deve conter somente o CSV com nome idêntico ao arquivo solicitado.")
            bad_member = archive.testzip()
            if bad_member is not None:
                raise DataQualityError("ZIP_CRC_FAILED", f"CRC inválido no membro {bad_member}.")
            with archive.open(expected_csv, "r") as binary:
                import io

                text = io.TextIOWrapper(binary, encoding="utf-8-sig", newline="")
                reader = csv.reader(text)
                try:
                    first = next(reader)
                except StopIteration:
                    raise DataQualityError("SCHEMA_INVALID", "CSV de klines vazio.") from None
                if _is_native_header(first):
                    header_present = True
                    first = None  # the recognized header is the only row skipped
                rows: Iterable[Sequence[str]] = reader if first is None else _chain_first(first, reader)
                row_number = 1 if header_present else 0
                for row_number, fields in enumerate(rows, start=2 if header_present else 1):
                    source_rows += 1
                    try:
                        parsed = _parse_row(fields, row_number=row_number, raw=raw, unit=unit, delay_ms=delay_ms)
                    except DataQualityError as error:
                        invalid_rows += 1
                        if len(errors) < 100:
                            errors.append({"row": row_number, "code": error.code, "message": str(error)})
                        continue
                    if last_open is not None and parsed.open_at < last_open:
                        out_of_order += 1
                    last_open = parsed.open_at
                    previous = bars.get(parsed.open_at)
                    if previous is None:
                        bars[parsed.open_at] = parsed
                    elif previous.canonical_values() == parsed.canonical_values():
                        duplicate_count += 1
                    else:
                        invalid_rows += 1
                        errors.append({
                            "row": row_number,
                            "code": "CONFLICTING_DUPLICATE",
                            "message": f"open_time duplicado com conteúdo conflitante: {_utc_text(parsed.open_at)}.",
                        })
                        break
    except DataQualityError:
        raise
    except (OSError, zipfile.BadZipFile, UnicodeError, csv.Error) as error:
        raise DataQualityError("SCHEMA_INVALID", f"Falha ao ler ZIP/CSV: {error}") from error

    ordered = sorted(bars.values(), key=lambda bar: bar.open_at)
    if out_of_order:
        errors.append({"code": "OUT_OF_ORDER", "count": out_of_order, "message": "Linhas fora da ordem temporal de origem."})
    start = _as_utc(datetime.fromisoformat(raw["period_start_utc"].replace("Z", "+00:00")))
    end = _as_utc(datetime.fromisoformat(raw["period_end_utc"].replace("Z", "+00:00")))
    expected = int((end - start) / MINUTE)
    observed = len(ordered)
    gap_count = max(0, expected - observed)
    first_at = ordered[0].open_at if ordered else None
    last_at = ordered[-1].open_at if ordered else None
    return {
        "timestamp_unit": unit,
        "header_present": header_present,
        "source_rows": source_rows,
        "invalid_rows_count": invalid_rows,
        "valid_rows": observed,
        "identical_duplicate_rows": duplicate_count,
        "out_of_order_rows": out_of_order,
        "expected_rows": expected,
        "gap_count": gap_count,
        "missing_1m_ranges": _missing_minute_ranges(ordered, start, end),
        "coverage_pct": round(observed * 100 / expected, 8) if expected else 0.0,
        "first_open_at_utc": _utc_text(first_at) if first_at else None,
        "last_open_at_utc": _utc_text(last_at) if last_at else None,
        "period_start_utc": _utc_text(start),
        "period_end_utc_exclusive": _utc_text(end),
        "errors": errors,
        "bars": ordered,
    }


def _chain_first(first: Sequence[str], rest: Iterable[Sequence[str]]) -> Iterable[Sequence[str]]:
    yield first
    yield from rest


def _ensure_f2_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS curated_partitions (
            partition_id TEXT PRIMARY KEY,
            raw_object_id TEXT NOT NULL REFERENCES raw_objects(object_id),
            symbol TEXT NOT NULL,
            market_type TEXT NOT NULL CHECK (market_type = 'SPOT'),
            timeframe TEXT NOT NULL CHECK (timeframe IN ('1m','1h','4h')),
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            timestamp_unit TEXT NOT NULL CHECK (timestamp_unit IN ('ms','us')),
            delay_assumed_ms INTEGER NOT NULL CHECK (delay_assumed_ms > 0),
            parser_version TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            qa_status TEXT NOT NULL CHECK (qa_status = 'PASS'),
            row_count INTEGER NOT NULL,
            expected_count INTEGER NOT NULL,
            gap_count INTEGER NOT NULL,
            coverage_pct TEXT NOT NULL,
            duplicate_rows_deduplicated INTEGER NOT NULL,
            first_open_at_utc TEXT,
            last_open_at_utc TEXT,
            parquet_path TEXT NOT NULL,
            logical_sha256 TEXT NOT NULL,
            parquet_sha256 TEXT NOT NULL,
            qa_json_path TEXT NOT NULL,
            qa_html_path TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            UNIQUE(raw_object_id, timeframe, parser_version, schema_version, delay_assumed_ms)
        );
        CREATE INDEX IF NOT EXISTS curated_partitions_by_period
            ON curated_partitions(symbol, timeframe, year, month, qa_status);
        CREATE TABLE IF NOT EXISTS data_quarantine (
            quarantine_id TEXT PRIMARY KEY,
            raw_object_id TEXT NOT NULL REFERENCES raw_objects(object_id),
            reason_code TEXT NOT NULL,
            reason TEXT NOT NULL,
            quarantine_path TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS dataset_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            market_type TEXT NOT NULL CHECK (market_type = 'SPOT'),
            data_origin TEXT NOT NULL CHECK (data_origin IN ('REAL','SYNTHETIC')),
            qa_status TEXT NOT NULL CHECK (qa_status = 'PASS'),
            parser_version TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            delay_assumed_ms INTEGER NOT NULL CHECK (delay_assumed_ms > 0),
            content_sha256 TEXT NOT NULL UNIQUE,
            source_partition_count INTEGER NOT NULL,
            manifest_path TEXT NOT NULL,
            qa_json_path TEXT NOT NULL,
            qa_html_path TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS snapshot_sources (
            snapshot_id TEXT NOT NULL REFERENCES dataset_snapshots(snapshot_id),
            ordinal INTEGER NOT NULL,
            partition_id TEXT NOT NULL REFERENCES curated_partitions(partition_id),
            PRIMARY KEY(snapshot_id, partition_id),
            UNIQUE(snapshot_id, ordinal)
        );
        """
    )


@contextmanager
def _catalog(root: Path) -> Iterable[sqlite3.Connection]:
    connection = _connect_catalog(root)
    try:
        _ensure_f2_schema(connection)
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _replace_with_retry(source: Path, destination: Path) -> None:
    """Retry transient Windows sharing violations while publishing a file."""
    for attempt in range(6):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.05 * (2 ** attempt))


def _write_json_new(path: Path, value: Any) -> None:
    """Create an immutable artifact without replacing any existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_immutable_text(path: Path, content: str) -> None:
    """Create a versioned QA artifact once; identical reruns are idempotent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_text(encoding="utf-8") == content:
            return
        raise DataQualityError(
            "QA_ARTIFACT_VERSION_CONFLICT",
            f"Artefato QA existente diverge do conteúdo versionado: {path.as_posix()}.",
        )


def _render_quality_html(report: dict[str, Any]) -> str:
    rows = []
    for partition in report.get("partitions", []):
        rows.append(
            "<tr>"
            + "".join(
                f"<td>{html.escape(str(partition.get(key, '')))}</td>"
                for key in ("timeframe", "qa_status", "row_count", "expected_count", "gap_count", "coverage_pct", "partition_id")
            )
            + "</tr>"
        )
    error_rows = "".join(
        f"<li><b>{html.escape(str(item.get('code', 'ERROR')))}</b>: {html.escape(str(item.get('message', '')))}</li>"
        for item in report.get("errors", [])
    )
    open_windows = report.get("incomplete_windows", [])
    window_rows = "".join(
        f"<tr><td>{html.escape(str(item['timeframe']))}</td><td>{html.escape(str(item['window_open_at_utc']))}</td>"
        f"<td>{item['observed_count']}</td><td>{item['expected_count']}</td><td>{item['missing_count']}</td></tr>"
        for item in open_windows[:200]
    )
    gap_rows = "".join(
        f"<tr><td>{html.escape(item['start_at_utc'])}</td><td>{html.escape(item['end_at_utc_exclusive'])}</td><td>{item['missing_minutes']}</td></tr>"
        for item in report.get("missing_1m_ranges", [])
    )
    return f"""<!doctype html><html lang="pt-BR"><meta charset="utf-8"><title>F2 QA {html.escape(report['raw_object_id'])}</title>
<style>body{{font:15px system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#18212b}}table{{border-collapse:collapse;width:100%;margin:1rem 0}}th,td{{border:1px solid #cbd5e1;padding:.45rem;text-align:left}}th{{background:#e8eef5}}.notice{{background:#fff4ce;padding:.8rem}}</style>
<h1>Relatório de qualidade F2</h1><p><b>Raw object:</b> {html.escape(report['raw_object_id'])}</p>
<p><b>QA:</b> {html.escape(report['qa_status'])} · <b>Período:</b> {html.escape(report['period_start_utc'])} — {html.escape(report['period_end_utc_exclusive'])}</p>
<p><b>SHA-256 raw:</b> {html.escape(report['raw_sha256'])} · <b>Unidade explícita:</b> {html.escape(report.get('timestamp_unit', 'n/a'))}</p>
<p><b>Delay:</b> {report['delay_assumed_ms']} ms, disponibilidade ASSUMED.</p>
<p class="notice">Cobertura e custos de qualidade descrevem os arquivos observados. A disponibilidade histórica é uma hipótese de atraso, não latência medida. Um candle agregado só recebe PASS com todos os minutos esperados; janelas incompletas são bloqueadas, sem preenchimento.</p>
<h2>Partições</h2><table><thead><tr><th>Timeframe</th><th>QA</th><th>Observados</th><th>Esperados</th><th>Lacunas/janelas bloqueadas</th><th>Cobertura %</th><th>Partition ID</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<h2>Janelas agregadas incompletas</h2><table><thead><tr><th>Timeframe</th><th>Abertura UTC</th><th>Observados</th><th>Esperados</th><th>Ausentes</th></tr></thead><tbody>{window_rows}</tbody></table>
<h2>Intervalos 1m ausentes</h2><table><thead><tr><th>Início UTC</th><th>Fim exclusivo UTC</th><th>Minutos ausentes</th></tr></thead><tbody>{gap_rows or '<tr><td colspan="3">Nenhum gap de 1m.</td></tr>'}</tbody></table>
<h2>Erros/quarentena</h2><ul>{error_rows or '<li>Nenhum erro de parsing ou conflito.</li>'}</ul>
<p><small>Parser {html.escape(report['parser_version'])}; schema {html.escape(report['schema_version'])}. Dados brutos permanecem no local imutável da F1.</small></p></html>"""


def _quarantine(root: Path, raw: sqlite3.Row, code: str, message: str, report: dict[str, Any]) -> str:
    identity = hashlib.sha256(f"{raw['object_id']}|{code}|{message}".encode("utf-8")).hexdigest()
    quarantine_id = f"q-{identity[:24]}"
    path = root / "data" / "quarantine" / "f2" / raw["object_id"] / f"{quarantine_id}.json"
    body = {
        "quarantine_id": quarantine_id,
        "raw_object_id": raw["object_id"],
        "raw_sha256": raw["sha256"],
        "reason_code": code,
        "reason": message,
        "raw_is_immutable_and_not_moved": True,
        "report": report,
    }
    _write_json_atomic(path, body)
    with _catalog(root) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO data_quarantine VALUES (?, ?, ?, ?, ?, ?)",
            (quarantine_id, raw["object_id"], code, message, path.relative_to(root).as_posix(), _utc_text(datetime.now(UTC))),
        )
    return path.relative_to(root).as_posix()


def _parquet_type_map_sql() -> str:
    return ", ".join(f"'{name}': '{PARQUET_TYPES[name]}'" for name in PARQUET_COLUMNS)


def _csv_row(bar: Kline) -> dict[str, str]:
    values = bar.canonical_values()
    return {
        name: _duck_timestamp_text(value) if isinstance(value, datetime) else format(value, "f") if isinstance(value, Decimal) else str(value)
        for name, value in zip(PARQUET_COLUMNS, values, strict=True)
    }


def _canonical_cell(value: Any) -> str:
    if isinstance(value, datetime):
        return _utc_text(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _logical_hash(connection: duckdb.DuckDBPyConnection, query: str) -> tuple[str, int, list[Any] | None, list[Any] | None]:
    digest = hashlib.sha256()
    cursor = connection.execute(query)
    count = 0
    first: list[Any] | None = None
    last: list[Any] | None = None
    while batch := cursor.fetchmany(4096):
        for row in batch:
            encoded = _canonical_json([_canonical_cell(value) for value in row])
            digest.update(encoded)
            digest.update(b"\n")
            if first is None:
                first = list(row)
            last = list(row)
            count += 1
    return digest.hexdigest(), count, first, last


def _publish_partition(
    *, root: Path, staging: Path, raw: sqlite3.Row, timeframe: str, delay_ms: int,
    row_count: int, expected_count: int, gap_count: int, coverage_pct: float,
    logical_sha: str, staged_file: Path, first: Sequence[Any] | None, last: Sequence[Any] | None,
    qa_json_path: str, qa_html_path: str, duplicate_count: int,
) -> dict[str, Any]:
    identity = {
        "raw_object_id": raw["object_id"],
        "raw_sha256": raw["sha256"],
        "timeframe": timeframe,
        "parser_version": PARSER_VERSION,
        "schema_version": CURATED_SCHEMA_VERSION,
        "timestamp_unit": declared_timestamp_unit(raw["year"], raw["month"]),
        "delay_assumed_ms": delay_ms,
        "logical_sha256": logical_sha,
    }
    partition_digest = hashlib.sha256(_canonical_json(identity)).hexdigest()
    partition_id = f"curated-{timeframe}-{partition_digest[:24]}"
    relative_path = Path(
        "data", "curated", "binance", "spot", raw["symbol"], timeframe,
        f"{raw['year']:04d}-{raw['month']:02d}", f"{partition_id}.parquet",
    )
    target = root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        _replace_with_retry(staged_file, target)
    elif staged_file.exists():
        staged_file.unlink()
    parquet_sha = _sha256_file(target)
    first_at = _utc_text(first[0]) if first else None
    last_at = _utc_text(last[0]) if last else None
    return {
        **identity,
        "partition_id": partition_id,
        "symbol": raw["symbol"],
        "market_type": "SPOT",
        "year": raw["year"],
        "month": raw["month"],
        "qa_status": "PASS",
        "row_count": row_count,
        "expected_count": expected_count,
        "gap_count": gap_count,
        "coverage_pct": f"{coverage_pct:.8f}",
        "duplicate_rows_deduplicated": duplicate_count if timeframe == "1m" else 0,
        "first_open_at_utc": first_at,
        "last_open_at_utc": last_at,
        "parquet_path": relative_path.as_posix(),
        "parquet_sha256": parquet_sha,
        "qa_json_path": qa_json_path,
        "qa_html_path": qa_html_path,
    }


def _store_partitions(root: Path, partitions: Sequence[dict[str, Any]]) -> None:
    if not partitions:
        return
    with _catalog(root) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            for item in partitions:
                existing = connection.execute(
                    "SELECT parquet_sha256, logical_sha256, qa_json_path, qa_html_path "
                    "FROM curated_partitions WHERE partition_id = ?",
                    (item["partition_id"],),
                ).fetchone()
                if existing is not None:
                    if (existing["parquet_sha256"], existing["logical_sha256"]) != (
                        item["parquet_sha256"], item["logical_sha256"]
                    ):
                        raise DataQualityError("CURATED_ID_CONFLICT", f"partition_id já existe com conteúdo diferente: {item['partition_id']}.")
                    if (existing["qa_json_path"], existing["qa_html_path"]) != (
                        item["qa_json_path"], item["qa_html_path"]
                    ):
                        connection.execute(
                            "UPDATE curated_partitions SET qa_json_path = ?, qa_html_path = ? WHERE partition_id = ?",
                            (item["qa_json_path"], item["qa_html_path"], item["partition_id"]),
                        )
                    continue
                connection.execute(
                    """INSERT INTO curated_partitions (
                        partition_id, raw_object_id, symbol, market_type, timeframe, year, month,
                        timestamp_unit, delay_assumed_ms, parser_version, schema_version, qa_status,
                        row_count, expected_count, gap_count, coverage_pct, duplicate_rows_deduplicated,
                        first_open_at_utc, last_open_at_utc, parquet_path, logical_sha256, parquet_sha256,
                        qa_json_path, qa_html_path, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        item["partition_id"], item["raw_object_id"], item["symbol"], item["market_type"],
                        item["timeframe"], item["year"], item["month"], item["timestamp_unit"],
                        item["delay_assumed_ms"], item["parser_version"], item["schema_version"],
                        item["qa_status"], item["row_count"], item["expected_count"], item["gap_count"],
                        item["coverage_pct"], item["duplicate_rows_deduplicated"], item["first_open_at_utc"],
                        item["last_open_at_utc"], item["parquet_path"], item["logical_sha256"],
                        item["parquet_sha256"], item["qa_json_path"], item["qa_html_path"], _utc_text(datetime.now(UTC)),
                    ),
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def _incomplete_window_summary(
    connection: duckdb.DuckDBPyConnection,
    timeframe: str,
    minutes: int,
    start: datetime,
    end: datetime,
) -> tuple[list[dict[str, Any]], int, int]:
    bucket = f"time_bucket(INTERVAL '{minutes} minutes', open_at_utc)"
    query = f"""
        SELECT {bucket} AS window_open_at_utc,
               count(*)::BIGINT AS observed_count,
               count(DISTINCT open_at_utc)::BIGINT AS distinct_count,
               min(open_at_utc) AS first_at,
               max(open_at_utc) AS last_at
        FROM minute_bars
        GROUP BY 1
        ORDER BY 1
    """
    rows = connection.execute(query).fetchall()
    by_open = {
        _as_utc(row[0]): (row[1], row[2], _as_utc(row[3]), _as_utc(row[4]))
        for row in rows
    }
    incomplete: list[dict[str, Any]] = []
    complete_count = 0
    expected = minutes
    window_open = start
    while window_open < end:
        observed, distinct_count, first_at, last_at = by_open.get(window_open, (0, 0, None, None))
        complete = (
            observed == expected
            and distinct_count == expected
            and first_at == window_open
            and last_at == window_open + timedelta(minutes=minutes - 1)
        )
        if complete:
            complete_count += 1
        else:
            incomplete.append({
                "timeframe": timeframe,
                "window_open_at_utc": _utc_text(window_open),
                "observed_count": observed,
                "expected_count": expected,
                "missing_count": max(0, expected - distinct_count),
                "status": "INCOMPLETE_BLOCKED",
            })
        window_open += timedelta(minutes=minutes)
    return incomplete, complete_count, int((end - start) / timedelta(minutes=minutes))


def _aggregation_query(timeframe: str, minutes: int, delay_ms: int) -> str:
    bucket = f"time_bucket(INTERVAL '{minutes} minutes', open_at_utc)"
    return f"""
        WITH bucketed AS (
            SELECT *, {bucket} AS bucket_open
            FROM minute_bars
        ), grouped AS (
            SELECT bucket_open,
                   count(*)::BIGINT AS observed_count,
                   count(DISTINCT open_at_utc)::BIGINT AS distinct_count,
                   min(open_at_utc) AS first_at,
                   max(open_at_utc) AS last_at,
                   arg_min(open, open_at_utc) AS open,
                   max(high) AS high,
                   min(low) AS low,
                   arg_max(close, open_at_utc) AS close,
                   sum(volume_base)::DECIMAL(38,18) AS volume_base,
                   sum(quote_volume)::DECIMAL(38,18) AS quote_volume,
                   sum(trade_count)::BIGINT AS trade_count,
                   sum(taker_buy_base_volume)::DECIMAL(38,18) AS taker_buy_base_volume,
                   sum(taker_buy_quote_volume)::DECIMAL(38,18) AS taker_buy_quote_volume,
                   arg_max(ignore_value, open_at_utc) AS ignore_value,
                   max(source_close_time_raw)::BIGINT AS source_close_time_raw,
                   max(source_close_at_utc) AS source_close_at_utc,
                   arg_max(raw_object_id, open_at_utc) AS raw_object_id,
                   arg_max(timestamp_unit, open_at_utc) AS timestamp_unit
            FROM bucketed
            GROUP BY bucket_open
        )
        SELECT bucket_open AS open_at_utc,
               bucket_open + INTERVAL '{minutes} minutes' AS end_at_utc,
               bucket_open + INTERVAL '{minutes} minutes' + INTERVAL '{delay_ms} milliseconds' AS available_at_utc,
               source_close_time_raw,
               source_close_at_utc,
               timestamp_unit,
               open, high, low, close, volume_base, quote_volume, trade_count,
               taker_buy_base_volume, taker_buy_quote_volume, ignore_value,
               'PASS' AS quality_status, raw_object_id
        FROM grouped
        WHERE observed_count = {minutes}
          AND distinct_count = {minutes}
          AND first_at = bucket_open
          AND last_at = bucket_open + INTERVAL '{minutes - 1} minutes'
        ORDER BY bucket_open
    """


def _make_quarantine_report(raw: sqlite3.Row, delay_ms: int, code: str, message: str) -> dict[str, Any]:
    start = _as_utc(datetime.fromisoformat(raw["period_start_utc"].replace("Z", "+00:00")))
    end = _as_utc(datetime.fromisoformat(raw["period_end_utc"].replace("Z", "+00:00")))
    return {
        "report_version": QA_REPORT_VERSION,
        "raw_object_id": raw["object_id"],
        "raw_sha256": raw["sha256"],
        "symbol": raw["symbol"],
        "market_type": "SPOT",
        "period_start_utc": _utc_text(start),
        "period_end_utc_exclusive": _utc_text(end),
        "timestamp_unit": declared_timestamp_unit(raw["year"], raw["month"]),
        "delay_assumed_ms": delay_ms,
        "available_at_confidence": "ASSUMED",
        "parser_version": PARSER_VERSION,
        "schema_version": CURATED_SCHEMA_VERSION,
        "qa_status": "QUARANTINED",
        "errors": [{"code": code, "message": message}],
        "partitions": [],
        "incomplete_windows": [],
    }


def _write_audit_report(root: Path, report: dict[str, Any]) -> tuple[str, str]:
    json_rel, html_rel = _audit_report_paths(report)
    _write_immutable_text(
        root / Path(json_rel),
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    _write_immutable_text(root / Path(html_rel), _render_quality_html(report))
    return json_rel, html_rel


def _audit_report_paths(report: dict[str, Any]) -> tuple[str, str]:
    report_key = hashlib.sha256(
        _canonical_json({
            "report_version": report["report_version"],
            "raw_object_id": report["raw_object_id"],
            "raw_sha256": report["raw_sha256"],
            "parser_version": report["parser_version"],
            "schema_version": report["schema_version"],
            "delay_assumed_ms": report["delay_assumed_ms"],
            "qa_status": report["qa_status"],
        })
    ).hexdigest()
    base = Path("artifacts", "data-quality", report["raw_object_id"], report_key)
    json_path = base.with_suffix(".json")
    html_path = base.with_suffix(".html")
    return json_path.as_posix(), html_path.as_posix()


def normalize_and_audit_raw_object(root: Path, raw_object_id: str, *, delay_ms: int = DEFAULT_DELAY_MS) -> dict[str, Any]:
    """Read one exact F1 object, validate it, write versioned Parquet and QA reports."""
    if delay_ms <= 0:
        raise ValueError("delay_ms deve ser positivo; available_at é ASSUMED.")
    with _catalog(root) as connection:
        raw = connection.execute("SELECT * FROM raw_objects WHERE object_id = ?", (raw_object_id,)).fetchone()
        if raw is None:
            raise DataQualityError("RAW_OBJECT_NOT_FOUND", f"raw_object_id não encontrado: {raw_object_id}.")

    start = _as_utc(datetime.fromisoformat(raw["period_start_utc"].replace("Z", "+00:00")))
    end = _as_utc(datetime.fromisoformat(raw["period_end_utc"].replace("Z", "+00:00")))
    report = {
        "report_version": QA_REPORT_VERSION,
        "raw_object_id": raw["object_id"],
        "raw_sha256": raw["sha256"],
        "symbol": raw["symbol"],
        "market_type": raw["market_type"],
        "period_start_utc": _utc_text(start),
        "period_end_utc_exclusive": _utc_text(end),
        "delay_assumed_ms": delay_ms,
        "available_at_confidence": "ASSUMED",
        "parser_version": PARSER_VERSION,
        "schema_version": CURATED_SCHEMA_VERSION,
        "parquet_timestamp_convention": "TIMESTAMP values are timezone-naive UTC; all *_utc fields are interpreted as UTC",
        "source_checksum_status": raw["checksum_status"],
        "source_status": raw["status"],
        "qa_status": "FAIL",
        "partitions": [],
        "incomplete_windows": [],
        "errors": [],
    }
    raw_path = root / Path(raw["raw_path"])
    try:
        if raw["market_type"] != "SPOT" or raw["interval"] != "1m":
            raise DataQualityError("MARKET_OR_INTERVAL", "F2 aceita apenas Binance Spot klines 1m de F1.")
        if raw["status"] != "VERIFIED":
            raise DataQualityError("RAW_NOT_VERIFIED", f"Raw F1 está com status {raw['status']}.")
        if not raw_path.is_file():
            raise DataQualityError("RAW_MISSING", f"ZIP raw não encontrado: {raw['raw_path']}.")
        actual_sha = _sha256_file(raw_path)
        if actual_sha != raw["sha256"]:
            raise DataQualityError("RAW_HASH_MISMATCH", "SHA-256 do ZIP mudou desde o registro F1.")
        if raw["checksum_status"] != "VERIFIED":
            raise DataQualityError("CHECKSUM_UNVERIFIED", "Snapshot F2 requer checksum validado pela F1; fonte sem checksum fica bloqueada.")
        checksum_path = root / Path(raw["checksum_path"]) if raw["checksum_path"] else None
        if checksum_path is None or not checksum_path.is_file():
            raise DataQualityError("CHECKSUM_MISSING", "O objeto F1 diz VERIFIED, mas seu arquivo CHECKSUM não está presente.")
        manifest_path = root / Path(raw["manifest_path"])
        source_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if source_manifest.get("checksum_sha256") != raw["sha256"]:
            raise DataQualityError("CHECKSUM_MISMATCH", "Manifesto F1 e catálogo divergem quanto ao hash do ZIP.")
        checksum_bytes = checksum_path.read_bytes()
        if source_manifest.get("checksum_file_sha256") and hashlib.sha256(checksum_bytes).hexdigest() != source_manifest["checksum_file_sha256"]:
            raise DataQualityError("CHECKSUM_FILE_CHANGED", "Arquivo CHECKSUM mudou desde a validação F1.")
        checksum_fields = checksum_bytes.decode("ascii").strip().split()
        if not checksum_fields or checksum_fields[0].lower() != raw["sha256"].lower():
            raise DataQualityError("CHECKSUM_MISMATCH", "O CHECKSUM presente não corresponde ao SHA-256 raw do catálogo.")
        if len(checksum_fields) > 1 and checksum_fields[-1].lstrip("*") != raw["original_zip_name"]:
            raise DataQualityError("CHECKSUM_SCHEMA", "O nome no CHECKSUM não corresponde ao ZIP do objeto F1.")
        report["checksum_origin"] = source_manifest.get("checksum_origin", "UNKNOWN")
        report["timestamp_unit"] = declared_timestamp_unit(raw["year"], raw["month"])
        parsed = _parse_archive(raw_path, raw, delay_ms)
        report.update({key: value for key, value in parsed.items() if key != "bars"})
        if parsed["errors"]:
            report["qa_status"] = "QUARANTINED"
            report["errors"] = parsed["errors"]
            raise DataQualityError(parsed["errors"][0]["code"], parsed["errors"][0]["message"])
        bars: list[Kline] = parsed["bars"]
        if not bars:
            raise DataQualityError("SCHEMA_INVALID", "Nenhuma linha válida no CSV.")

        staging_root = root / "data" / "curated" / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"{raw_object_id[:20]}-", dir=staging_root) as temp_dir:
            staging = Path(temp_dir)
            input_csv = staging / "normalized_1m.csv"
            with input_csv.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=PARQUET_COLUMNS, lineterminator="\n")
                writer.writeheader()
                writer.writerows(_csv_row(bar) for bar in bars)

            db = duckdb.connect(database=":memory:")
            try:
                columns_sql = _parquet_type_map_sql()
                path_sql = str(input_csv.resolve()).replace("\\", "/").replace("'", "''")
                db.execute(
                    f"CREATE TABLE minute_bars AS SELECT * FROM read_csv('{path_sql}', header=true, auto_detect=false, columns={{{columns_sql}}})"
                )
                one_minute_query = "SELECT " + ", ".join(PARQUET_COLUMNS) + " FROM minute_bars ORDER BY open_at_utc"
                one_hash, one_count, one_first, one_last = _logical_hash(db, one_minute_query)
                one_parquet = staging / "1m.parquet"
                parquet_sql = str(one_parquet.resolve()).replace("\\", "/").replace("'", "''")
                db.execute(f"COPY ({one_minute_query}) TO '{parquet_sql}' (FORMAT PARQUET, COMPRESSION ZSTD)")
                expected_1m = parsed["expected_rows"]
                incomplete: list[dict[str, Any]] = []
                partition_specs: list[dict[str, Any]] = [
                    {
                        "timeframe": "1m", "staged_file": one_parquet, "query": one_minute_query,
                        "row_count": one_count, "expected_count": expected_1m,
                        "gap_count": expected_1m - one_count,
                        "coverage_pct": one_count * 100 / expected_1m,
                        "logical_sha": one_hash, "first": one_first, "last": one_last,
                    }
                ]
                aggregate_summary: dict[str, dict[str, int]] = {}
                for timeframe, minutes in (("1h", 60), ("4h", 240)):
                    incomplete_rows, complete_count, expected_windows = _incomplete_window_summary(
                        db, timeframe, minutes, start, end
                    )
                    incomplete.extend(incomplete_rows)
                    aggregate_summary[timeframe] = {
                        "complete": complete_count,
                        "expected": expected_windows,
                        "incomplete": expected_windows - complete_count,
                    }
                    query = _aggregation_query(timeframe, minutes, delay_ms)
                    logical_hash, row_count, first, last = _logical_hash(db, query)
                    if row_count == 0:
                        report["partitions"].append({
                            "timeframe": timeframe,
                            "qa_status": "NO_COMPLETE_WINDOWS",
                            "row_count": 0,
                            "expected_count": expected_windows,
                            "gap_count": expected_windows,
                            "coverage_pct": 0.0,
                            "partition_id": None,
                        })
                        continue
                    output = staging / f"{timeframe}.parquet"
                    parquet_sql = str(output.resolve()).replace("\\", "/").replace("'", "''")
                    db.execute(f"COPY ({query}) TO '{parquet_sql}' (FORMAT PARQUET, COMPRESSION ZSTD)")
                    partition_specs.append({
                        "timeframe": timeframe, "staged_file": output, "query": query,
                        "row_count": row_count, "expected_count": expected_windows,
                        "gap_count": max(0, expected_windows - row_count),
                        "coverage_pct": row_count * 100 / expected_windows if expected_windows else 0,
                        "logical_sha": logical_hash, "first": first, "last": last,
                    })
                report["partitions"] = [
                    {
                        "timeframe": item["timeframe"],
                        "qa_status": "PASS",
                        "row_count": item["row_count"],
                        "expected_count": item["expected_count"],
                        "gap_count": item["gap_count"],
                        "coverage_pct": round(item["coverage_pct"], 8),
                        "partition_id": None,
                    }
                    for item in partition_specs
                ] + report["partitions"]
                report["incomplete_windows"] = incomplete
                report["aggregate_windows"] = aggregate_summary
            finally:
                db.close()

            report["qa_status"] = "PASS"
            json_rel, html_rel = _audit_report_paths(report)
            published: list[dict[str, Any]] = []
            for spec in partition_specs:
                item = _publish_partition(
                    root=root,
                    staging=staging,
                    raw=raw,
                    timeframe=spec["timeframe"],
                    delay_ms=delay_ms,
                    row_count=spec["row_count"],
                    expected_count=spec["expected_count"],
                    gap_count=spec["gap_count"],
                    coverage_pct=spec["coverage_pct"],
                    logical_sha=spec["logical_sha"],
                    staged_file=spec["staged_file"],
                    first=spec["first"],
                    last=spec["last"],
                    qa_json_path=json_rel,
                    qa_html_path=html_rel,
                    duplicate_count=parsed["identical_duplicate_rows"],
                )
                published.append(item)
                report_partition = next(part for part in report["partitions"] if part.get("timeframe") == item["timeframe"])
                report_partition.update({key: item[key] for key in ("partition_id", "parquet_path", "logical_sha256", "parquet_sha256")})
            report["partitions"] = [
                next((item for item in published if item["timeframe"] == p["timeframe"]), p)
                for p in report["partitions"]
            ]
            json_rel, html_rel = _write_audit_report(root, report)
            for item in published:
                item["qa_json_path"] = json_rel
                item["qa_html_path"] = html_rel
            _store_partitions(root, published)
            return {**report, "qa_json_path": json_rel, "qa_html_path": html_rel}
    except DataQualityError as error:
        report["qa_status"] = "QUARANTINED"
        if not report.get("errors") or report["errors"][0].get("code") != error.code:
            report["errors"].append({"code": error.code, "message": str(error)})
        json_rel, html_rel = _write_audit_report(root, report)
        quarantine_path = _quarantine(root, raw, error.code, str(error), report)
        return {
            **report,
            "qa_json_path": json_rel,
            "qa_html_path": html_rel,
            "quarantine_path": quarantine_path,
        }


def list_raw_objects(root: Path, *, symbol: str | None = None, raw_object_ids: Sequence[str] = ()) -> list[dict[str, Any]]:
    with _catalog(root) as connection:
        if raw_object_ids:
            placeholders = ",".join("?" for _ in raw_object_ids)
            rows = connection.execute(
                f"SELECT * FROM raw_objects WHERE object_id IN ({placeholders}) ORDER BY year, month, revision, object_id",
                list(raw_object_ids),
            ).fetchall()
            found = {row["object_id"] for row in rows}
            missing = set(raw_object_ids) - found
            if missing:
                raise DataQualityError("RAW_OBJECT_NOT_FOUND", f"raw_object_id(s) não encontrados: {', '.join(sorted(missing))}.")
        else:
            if symbol is None:
                raise ValueError("informe --symbol ou um --raw-object-id explícito")
            rows = connection.execute(
                "SELECT * FROM raw_objects WHERE symbol = ? ORDER BY year, month, revision, object_id", (symbol,)
            ).fetchall()
    return [dict(row) for row in rows]


def audit_raw_objects(
    root: Path,
    *,
    symbol: str | None = None,
    raw_object_ids: Sequence[str] = (),
    delay_ms: int = DEFAULT_DELAY_MS,
) -> list[dict[str, Any]]:
    objects = list_raw_objects(root, symbol=symbol, raw_object_ids=raw_object_ids)
    if not objects:
        raise DataQualityError("RAW_OBJECT_NOT_FOUND", "Nenhum raw_object F1 correspondente foi encontrado.")
    return [normalize_and_audit_raw_object(root, item["object_id"], delay_ms=delay_ms) for item in objects]


def list_partitions(root: Path, *, raw_object_ids: Sequence[str] = ()) -> list[dict[str, Any]]:
    with _catalog(root) as connection:
        if raw_object_ids:
            placeholders = ",".join("?" for _ in raw_object_ids)
            rows = connection.execute(
                f"SELECT * FROM curated_partitions WHERE raw_object_id IN ({placeholders}) ORDER BY year, month, timeframe, partition_id",
                list(raw_object_ids),
            ).fetchall()
        else:
            rows = connection.execute("SELECT * FROM curated_partitions ORDER BY year, month, timeframe, partition_id").fetchall()
    return [dict(row) for row in rows]


def create_snapshot(
    root: Path,
    partition_ids: Sequence[str],
    *,
    data_origin: str | None = None,
) -> dict[str, Any]:
    """Create immutable snapshot from explicitly listed PASS partition IDs."""
    if not partition_ids:
        raise ValueError("informe ao menos um --partition-id; não há seleção implícita/latest")
    if len(set(partition_ids)) != len(partition_ids):
        raise ValueError("partition_id repetido na seleção do snapshot")
    with _catalog(root) as connection:
        placeholders = ",".join("?" for _ in partition_ids)
        rows = connection.execute(
            f"""SELECT p.*, r.status AS raw_status, r.checksum_status AS raw_checksum_status,
                       r.raw_path AS raw_path, r.sha256 AS raw_sha256, r.acquisition_method,
                       r.manifest_path AS raw_manifest_path, r.revision AS raw_revision,
                       r.granularity AS source_granularity, r.day AS source_day,
                       r.original_zip_name AS source_archive_name,
                       r.period_start_utc AS source_period_start_utc,
                       r.period_end_utc AS source_period_end_utc
                FROM curated_partitions p JOIN raw_objects r ON r.object_id = p.raw_object_id
                WHERE p.partition_id IN ({placeholders})""",
            list(partition_ids),
        ).fetchall()
    found = {row["partition_id"] for row in rows}
    missing = set(partition_ids) - found
    if missing:
        raise DataQualityError("PARTITION_NOT_FOUND", f"partition_id(s) não encontrados: {', '.join(sorted(missing))}.")
    if any(row["qa_status"] != "PASS" for row in rows):
        raise DataQualityError("PARTITION_NOT_PASS", "Snapshot aceita somente partições QA PASS.")
    if not any(row["timeframe"] == "1m" for row in rows):
        raise DataQualityError("SNAPSHOT_REQUIRES_1M", "Snapshot F3 deve incluir ao menos uma partição 1m.")
    if len({(row["symbol"], row["parser_version"], row["schema_version"], row["delay_assumed_ms"]) for row in rows}) != 1:
        raise DataQualityError("SNAPSHOT_INCOMPATIBLE_PARTITIONS", "Partições divergem em instrumento, parser, schema ou delay.")
    for row in rows:
        parquet_path = root / Path(row["parquet_path"])
        if not parquet_path.is_file() or _sha256_file(parquet_path) != row["parquet_sha256"]:
            raise DataQualityError("PARQUET_HASH_MISMATCH", f"Parquet ausente ou alterado: {row['partition_id']}.")
        raw_path = root / Path(row["raw_path"])
        if not raw_path.is_file() or _sha256_file(raw_path) != row["raw_sha256"]:
            raise DataQualityError("RAW_HASH_MISMATCH", f"Raw ausente ou alterado: {row['raw_object_id']}.")
        if row["raw_status"] != "VERIFIED" or row["raw_checksum_status"] != "VERIFIED":
            raise DataQualityError("CHECKSUM_UNVERIFIED", f"Fonte raw não está VERIFIED: {row['raw_object_id']}.")

    timeframe_minutes = {"1m": 1, "1h": 60, "4h": 240}
    for timeframe in timeframe_minutes:
        timeframe_rows = sorted(
            (row for row in rows if row["timeframe"] == timeframe),
            key=lambda row: (row["first_open_at_utc"] or "", row["partition_id"]),
        )
        previous: sqlite3.Row | None = None
        for row in timeframe_rows:
            if previous is not None and previous["last_open_at_utc"] and row["first_open_at_utc"]:
                prior_end = _as_utc(datetime.fromisoformat(previous["last_open_at_utc"].replace("Z", "+00:00"))) + timedelta(minutes=timeframe_minutes[timeframe])
                current_start = _as_utc(datetime.fromisoformat(row["first_open_at_utc"].replace("Z", "+00:00")))
                if current_start < prior_end:
                    raise DataQualityError(
                        "SNAPSHOT_OVERLAP",
                        f"Partições sobrepostas em {timeframe}: {previous['partition_id']} "
                        f"(revisão {previous['raw_revision']}, {previous['source_granularity']}) e "
                        f"{row['partition_id']} (revisão {row['raw_revision']}, {row['source_granularity']}). "
                        "Escolha uma fonte/revisão para cada intervalo; nenhuma será descartada silenciosamente.",
                    )
            previous = row

    source_manifests = [json.loads((root / Path(row["raw_manifest_path"])).read_text(encoding="utf-8")) for row in rows]
    remote_official = all(
        row["acquisition_method"] == "remote_fetch"
        and manifest.get("checksum_origin") == "binance_public_data_download"
        for row, manifest in zip(rows, source_manifests, strict=True)
    )
    if data_origin not in (None, "REAL", "SYNTHETIC"):
        raise ValueError("data_origin deve ser REAL ou SYNTHETIC")
    if data_origin is None:
        if not remote_official:
            raise DataQualityError("DATA_ORIGIN_REQUIRED", "Para arquivos offline, classifique explicitamente --data-origin REAL ou SYNTHETIC.")
        data_origin = "REAL"
    selected = sorted(rows, key=lambda item: (item["timeframe"], item["first_open_at_utc"] or "", item["partition_id"]))
    partition_payload = [
        {
            "partition_id": row["partition_id"],
            "raw_object_id": row["raw_object_id"],
            "raw_sha256": row["raw_sha256"],
            "timeframe": row["timeframe"],
            "parquet_path": row["parquet_path"],
            "parquet_sha256": row["parquet_sha256"],
            "logical_sha256": row["logical_sha256"],
            "qa_status": row["qa_status"],
            "year": row["year"],
            "month": row["month"],
            "row_count": row["row_count"],
            "expected_count": row["expected_count"],
            "gap_count": row["gap_count"],
            "coverage_pct": row["coverage_pct"],
            "raw_revision": row["raw_revision"],
            "source_granularity": row["source_granularity"],
            "source_archive_name": row["source_archive_name"],
            "source_period_start_utc": row["source_period_start_utc"],
            "source_period_end_utc": row["source_period_end_utc"],
            "first_open_at_utc": row["first_open_at_utc"],
            "last_open_at_utc": row["last_open_at_utc"],
        }
        for row in selected
    ]
    content = {
        "symbol": selected[0]["symbol"],
        "market_type": "SPOT",
        "data_origin": data_origin,
        "parser_version": PARSER_VERSION,
        "schema_version": CURATED_SCHEMA_VERSION,
        "delay_assumed_ms": selected[0]["delay_assumed_ms"],
        "sources": partition_payload,
    }
    content_sha = hashlib.sha256(_canonical_json(content)).hexdigest()
    snapshot_id = f"snapshot-{content_sha[:32]}"
    base = Path("data", "curated", "snapshots", snapshot_id)
    created = _utc_text(datetime.now(UTC))
    selected_ids = {row["partition_id"] for row in selected}
    selected_ranges = [
        (
            _as_utc(datetime.fromisoformat(row["first_open_at_utc"].replace("Z", "+00:00"))),
            _as_utc(datetime.fromisoformat(row["last_open_at_utc"].replace("Z", "+00:00"))) + MINUTE,
            row["partition_id"],
        )
        for row in selected
        if row["timeframe"] == "1m" and row["first_open_at_utc"] and row["last_open_at_utc"]
    ]
    selected_symbol = selected[0]["symbol"]
    with _catalog(root) as connection:
        alternatives = connection.execute(
            """SELECT p.partition_id, p.raw_object_id, p.first_open_at_utc, p.last_open_at_utc,
                      r.revision, r.granularity, r.original_zip_name
               FROM curated_partitions p JOIN raw_objects r ON r.object_id = p.raw_object_id
               WHERE p.symbol=? AND p.timeframe='1m' AND p.qa_status='PASS'""",
            (selected_symbol,),
        ).fetchall()
    excluded_overlaps: list[dict[str, str]] = []
    for alternative in alternatives:
        if alternative["partition_id"] in selected_ids or not alternative["first_open_at_utc"] or not alternative["last_open_at_utc"]:
            continue
        alt_start = _as_utc(datetime.fromisoformat(alternative["first_open_at_utc"].replace("Z", "+00:00")))
        alt_end = _as_utc(datetime.fromisoformat(alternative["last_open_at_utc"].replace("Z", "+00:00"))) + MINUTE
        conflicts = [partition_id for start, end, partition_id in selected_ranges if alt_start < end and alt_end > start]
        if conflicts:
            excluded_overlaps.append({
                "partition_id": alternative["partition_id"],
                "raw_object_id": alternative["raw_object_id"],
                "revision": str(alternative["revision"]),
                "source_granularity": alternative["granularity"],
                "source_archive_name": alternative["original_zip_name"],
                "reason": "OVERLAPS_EXPLICITLY_SELECTED_PARTITION",
                "selected_partition_ids": ",".join(conflicts),
            })

    manifest = {
        "snapshot_id": snapshot_id,
        "content_sha256": content_sha,
        "created_at_utc": created,
        "qa_status": "PASS",
        "symbol": content["symbol"],
        "instrument_id": f"BINANCE:SPOT:{content['symbol']}",
        "market_type": "SPOT",
        "quote_currency": "USDT",
        "data_origin": data_origin,
        "source_integrity": "CHECKSUM_MATCH; authenticity depends on checksum origin",
        "checksum_origins": sorted({item.get("checksum_origin", "UNKNOWN") for item in source_manifests}),
        "parser_version": PARSER_VERSION,
        "schema_version": CURATED_SCHEMA_VERSION,
        "parquet_timestamp_convention": "TIMESTAMP values are timezone-naive UTC; all *_utc fields are interpreted as UTC",
        "delay_assumed_ms": content["delay_assumed_ms"],
        "available_at_confidence": "ASSUMED",
        "availability_policy": "end_at_utc + delay_assumed_ms; delay is a hypothesis, not observed latency",
        "sources": partition_payload,
        "excluded_overlapping_partitions": excluded_overlaps,
        "limitations": [
            "OHLCV candles are observations, not a fill guarantee",
            "available_at is an assumed historical availability time, not measured feed latency",
            "source checksum verifies byte correspondence; it does not authenticate an offline-provided checksum",
            "incomplete 1h/4h windows are omitted and listed as blocked in the QA reports",
        ],
    }
    partition_qa = [
        {
            "partition_id": row["partition_id"],
            "timeframe": row["timeframe"],
            "row_count": row["row_count"],
            "expected_count": row["expected_count"],
            "gap_count": row["gap_count"],
            "coverage_pct": row["coverage_pct"],
        }
        for row in selected
    ]
    snapshot_report = {
        "snapshot_id": snapshot_id,
        "content_sha256": content_sha,
        "qa_status": "PASS",
        "data_origin": data_origin,
        "sources": partition_qa,
        "exact_partition_ids": [row["partition_id"] for row in selected],
        "excluded_overlapping_partitions": excluded_overlaps,
        "unselected_latest_lookup": False,
    }
    snapshot_dir = root / base
    manifest_path = base / "snapshot.json"
    json_path = base / "qa.json"
    html_path = base / "qa.html"
    with _catalog(root) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT * FROM dataset_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        if existing is not None and existing["content_sha256"] != content_sha:
            raise DataQualityError("SNAPSHOT_ID_CONFLICT", "Snapshot ID existente com hash divergente.")
        if existing is not None:
            source_rows = connection.execute(
                "SELECT partition_id FROM snapshot_sources WHERE snapshot_id=? ORDER BY ordinal", (snapshot_id,)
            ).fetchall()
            _validate_snapshot_artifacts(root, existing, [row["partition_id"] for row in source_rows])
            return {
                "snapshot_id": snapshot_id,
                "content_sha256": content_sha,
                "qa_status": "PASS",
                "data_origin": data_origin,
                "partition_ids": [row["partition_id"] for row in selected],
                "manifest_path": manifest_path.as_posix(),
                "qa_json_path": json_path.as_posix(),
                "qa_html_path": html_path.as_posix(),
                "status": "SKIPPED_ALREADY_EXISTS",
            }
        if snapshot_dir.exists() and any(snapshot_dir.iterdir()):
            raise DataQualityError("SNAPSHOT_ARTIFACT_CONFLICT", "Há artefatos sem registro para este ID; nada será sobrescrito.")
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        created_paths: list[Path] = []
        try:
            _write_json_new(root / json_path, snapshot_report)
            created_paths.append(root / json_path)
            with (root / html_path).open("x", encoding="utf-8", newline="") as handle:
                handle.write(_render_snapshot_html(snapshot_report, manifest))
                handle.flush()
                os.fsync(handle.fileno())
            created_paths.append(root / html_path)
            _write_json_new(root / manifest_path, manifest)
            created_paths.append(root / manifest_path)
        except BaseException:
            for created_path in created_paths:
                created_path.unlink(missing_ok=True)
            try:
                snapshot_dir.rmdir()
            except OSError:
                pass
            raise
        if existing is None:
            connection.execute(
                """INSERT INTO dataset_snapshots VALUES (?, ?, 'SPOT', ?, 'PASS', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot_id, content["symbol"], data_origin, PARSER_VERSION, CURATED_SCHEMA_VERSION,
                    content["delay_assumed_ms"], content_sha, len(selected), manifest_path.as_posix(),
                    json_path.as_posix(), html_path.as_posix(), created,
                ),
            )
            connection.executemany(
                "INSERT INTO snapshot_sources(snapshot_id, ordinal, partition_id) VALUES (?, ?, ?)",
                [(snapshot_id, index, row["partition_id"]) for index, row in enumerate(selected)],
            )
            connection.commit()
    return {
        "snapshot_id": snapshot_id,
        "content_sha256": content_sha,
        "qa_status": "PASS",
        "data_origin": data_origin,
        "partition_ids": [row["partition_id"] for row in selected],
        "manifest_path": manifest_path.as_posix(),
        "qa_json_path": json_path.as_posix(),
        "qa_html_path": html_path.as_posix(),
    }


def _render_snapshot_html(report: dict[str, Any], manifest: dict[str, Any]) -> str:
    rows = "".join(
        f"<tr><td>{html.escape(item['timeframe'])}</td><td>{html.escape(item['partition_id'])}</td>"
        f"<td>{item['row_count']}</td><td>{item['expected_count']}</td><td>{item['gap_count']}</td>"
        f"<td>{html.escape(str(item['coverage_pct']))}</td></tr>"
        for item in report["sources"]
    )
    return f"""<!doctype html><html lang="pt-BR"><meta charset="utf-8"><title>Snapshot F2 {html.escape(report['snapshot_id'])}</title>
<style>body{{font:15px system-ui;max-width:1000px;margin:2rem auto;padding:0 1rem}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #cbd5e1;padding:.45rem;text-align:left}}.notice{{background:#fff4ce;padding:.8rem}}</style>
<h1>Snapshot F2 QA PASS</h1><p>ID: <code>{html.escape(report['snapshot_id'])}</code></p><p>Origem: {html.escape(report['data_origin'])}; símbolo: {html.escape(manifest['symbol'])} Spot USDT.</p>
<p class="notice">available_at é ASSUMED (end + {manifest['delay_assumed_ms']} ms), não latência historicamente medida. O snapshot fixa somente as partições enumeradas abaixo; dados OHLCV não garantem fills reais.</p>
<table><thead><tr><th>Timeframe</th><th>Partition ID</th><th>Observados</th><th>Esperados</th><th>Lacunas/bloqueios</th><th>Cobertura %</th></tr></thead><tbody>{rows}</tbody></table>
<p>SHA-256 conteúdo: {html.escape(report['content_sha256'])}. O manifesto registra os caminhos e hashes exatos.</p></html>"""


def _validate_snapshot_artifacts(
    root: Path,
    snapshot: sqlite3.Row,
    source_partition_ids: Sequence[str],
) -> dict[str, Any]:
    manifest_path = root / Path(snapshot["manifest_path"])
    qa_path = root / Path(snapshot["qa_json_path"])
    html_path = root / Path(snapshot["qa_html_path"])
    if not manifest_path.is_file() or not qa_path.is_file() or not html_path.is_file():
        raise DataQualityError("SNAPSHOT_ARTIFACT_MISSING", f"Artefatos ausentes para {snapshot['snapshot_id']}.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        qa = json.loads(qa_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DataQualityError("SNAPSHOT_ARTIFACT_INVALID", f"JSON do snapshot inválido: {error}.") from error
    content = {
        "symbol": manifest.get("symbol"),
        "market_type": manifest.get("market_type"),
        "data_origin": manifest.get("data_origin"),
        "parser_version": manifest.get("parser_version"),
        "schema_version": manifest.get("schema_version"),
        "delay_assumed_ms": manifest.get("delay_assumed_ms"),
        "sources": manifest.get("sources"),
    }
    manifest_ids = [item.get("partition_id") for item in manifest.get("sources", [])]
    digest = hashlib.sha256(_canonical_json(content)).hexdigest()
    if (
        manifest.get("snapshot_id") != snapshot["snapshot_id"]
        or manifest.get("content_sha256") != snapshot["content_sha256"]
        or digest != snapshot["content_sha256"]
        or manifest_ids != list(source_partition_ids)
        or qa.get("snapshot_id") != snapshot["snapshot_id"]
        or qa.get("content_sha256") != snapshot["content_sha256"]
        or qa.get("exact_partition_ids") != list(source_partition_ids)
        or qa.get("qa_status") != "PASS"
    ):
        raise DataQualityError("SNAPSHOT_HASH_MISMATCH", f"Manifesto, QA e catálogo divergem em {snapshot['snapshot_id']}.")
    return manifest


def load_backtest_snapshot(root: Path, snapshot_id: str) -> Any:
    """F2 reader bridge for F3: loads only the explicitly named snapshot's 1m PASS parts."""
    from quantlab.backtest import BacktestSnapshot

    with _catalog(root) as connection:
        snapshot = connection.execute("SELECT * FROM dataset_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        if snapshot is None:
            raise DataQualityError("SNAPSHOT_NOT_FOUND", f"snapshot_id não encontrado: {snapshot_id}.")
        all_sources = connection.execute(
            "SELECT partition_id FROM snapshot_sources WHERE snapshot_id=? ORDER BY ordinal", (snapshot_id,)
        ).fetchall()
        rows = connection.execute(
            """SELECT p.* FROM snapshot_sources s JOIN curated_partitions p USING(partition_id)
               WHERE s.snapshot_id=? AND p.timeframe='1m' ORDER BY p.first_open_at_utc, p.partition_id""",
            (snapshot_id,),
        ).fetchall()
    snapshot_manifest = _validate_snapshot_artifacts(
        root, snapshot, [row["partition_id"] for row in all_sources]
    )
    if not rows:
        raise DataQualityError("SNAPSHOT_REQUIRES_1M", "Snapshot não contém partições 1m.")
    paths: list[str] = []
    for row in rows:
        path = root / Path(row["parquet_path"])
        if not path.is_file() or _sha256_file(path) != row["parquet_sha256"]:
            raise DataQualityError("PARQUET_HASH_MISMATCH", f"Parquet alterado ou ausente: {row['partition_id']}.")
        paths.append(str(path.resolve()).replace("\\", "/").replace("'", "''"))
    path_list = ",".join("'" + path + "'" for path in paths)
    db = duckdb.connect(database=":memory:")
    try:
        candle_rows = db.execute(
            f"SELECT open_at_utc, end_at_utc, open, high, low, close, volume_base "
            f"FROM read_parquet([{path_list}]) ORDER BY open_at_utc"
        ).fetchall()
    finally:
        db.close()
    candles = tuple(
        Candle(
            open_at_utc=row[0].replace(tzinfo=UTC) if row[0].tzinfo is None else row[0].astimezone(UTC),
            end_at_utc=row[1].replace(tzinfo=UTC) if row[1].tzinfo is None else row[1].astimezone(UTC),
            open=Decimal(row[2]), high=Decimal(row[3]),
            low=Decimal(row[4]), close=Decimal(row[5]), volume_base=Decimal(row[6]),
            instrument_id=f"BINANCE:SPOT:{snapshot['symbol']}",
        )
        for row in candle_rows
    )
    metadata = {
        "partition_ids": ",".join(row["partition_id"] for row in all_sources),
        "candle_1m_partition_ids": ",".join(row["partition_id"] for row in rows),
        "parser_version": snapshot["parser_version"],
        "schema_version": snapshot["schema_version"],
        "delay_assumed_ms": str(snapshot["delay_assumed_ms"]),
        "available_at_confidence": "ASSUMED",
    }
    return BacktestSnapshot(
        snapshot_id=snapshot_id,
        instrument_id=f"BINANCE:SPOT:{snapshot['symbol']}",
        candles_1m=candles,
        content_sha256=snapshot["content_sha256"],
        qa_status=snapshot["qa_status"],
        data_origin=snapshot["data_origin"],
        source_metadata=metadata,
        source_manifest=snapshot_manifest,
    )
