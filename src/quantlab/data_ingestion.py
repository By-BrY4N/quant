"""Ingestão estrita de klines Spot públicas da Binance (F1/F5)."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

OFFICIAL_HOST = "data.binance.vision"
ALLOWED_SYMBOL = "BTCUSDT"
ALLOWED_SYMBOLS = ("BTCUSDT", "ETHUSDT")
ALLOWED_INTERVAL = "1m"
MIN_MONTH = (2017, 8)
MAX_MONTHS_PER_COMMAND = 3
MAX_DAYS_PER_COMMAND = 31
MAX_ARCHIVES_PER_COMMAND = 31
DEFAULT_MAX_TRANSFER_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
MAX_CHECKSUM_BYTES = 16 * 1024
MAX_REDIRECTS = 3
MAX_ATTEMPTS = 3
MONTH_PATTERN = re.compile(r"^(?P<year>\d{4})-(?P<month>0[1-9]|1[0-2])$")
CHECKSUM_PATTERN = re.compile(
    r"^(?P<digest>[0-9a-fA-F]{64})(?:[ \t]+\*?(?P<filename>[^\s]+))?$"
)


class IngestionError(ValueError):
    """Erro de validação ou aquisição que pode ser exibido diretamente na CLI."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        retry_count: int = 0,
        transferred_bytes: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retry_count = retry_count
        self.transferred_bytes = transferred_bytes


@dataclass(frozen=True, slots=True)
class MonthIdentity:
    symbol: str
    interval: str
    year: int
    month: int
    day: int | None = None

    @property
    def granularity(self) -> str:
        return "daily" if self.day is not None else "monthly"

    @property
    def key(self) -> str:
        period = f"{self.year:04d}-{self.month:02d}"
        if self.day is not None:
            period += f"-{self.day:02d}"
            return f"BINANCE:SPOT:{self.symbol}:{self.interval}:daily:{period}"
        return f"BINANCE:SPOT:{self.symbol}:{self.interval}:{period}"

    @property
    def zip_name(self) -> str:
        period = f"{self.year:04d}-{self.month:02d}"
        if self.day is not None:
            period += f"-{self.day:02d}"
        return f"{self.symbol}-{self.interval}-{period}.zip"

    @property
    def csv_name(self) -> str:
        return self.zip_name[:-4] + ".csv"

    @property
    def period_start_utc(self) -> str:
        return datetime(self.year, self.month, self.day or 1, tzinfo=UTC).isoformat().replace("+00:00", "Z")

    @property
    def period_end_utc(self) -> str:
        if self.day is not None:
            end = datetime(self.year, self.month, self.day, tzinfo=UTC) + timedelta(days=1)
            return end.isoformat().replace("+00:00", "Z")
        if self.month == 12:
            end = datetime(self.year + 1, 1, 1, tzinfo=UTC)
        else:
            end = datetime(self.year, self.month + 1, 1, tzinfo=UTC)
        return end.isoformat().replace("+00:00", "Z")

    @property
    def object_directory(self) -> Path:
        period = f"{self.year:04d}-{self.month:02d}"
        if self.day is not None:
            period += f"-{self.day:02d}"
        return Path(
            "data",
            "raw",
            "binance",
            "spot",
            self.granularity,
            "klines",
            self.symbol,
            self.interval,
            period,
        )


@dataclass(frozen=True, slots=True)
class IngestionResult:
    status: str
    object_id: str | None
    sha256: str | None
    byte_size: int | None
    raw_path: Path | None
    manifest_path: Path | None
    integrity_status: str | None
    revision: int | None
    retry_count: int = 0
    transferred_bytes: int = 0


@dataclass(slots=True)
class _TransferBudget:
    limit_bytes: int
    transferred_bytes: int = 0

    @property
    def remaining_bytes(self) -> int:
        return self.limit_bytes - self.transferred_bytes


@dataclass(frozen=True, slots=True)
class ZipValidation:
    entries: tuple[str, ...]
    uncompressed_bytes: int
    crc_verified: bool


def parse_month(value: str) -> tuple[int, int]:
    match = MONTH_PATTERN.fullmatch(value)
    if not match:
        raise IngestionError("INVALID_MONTH", f"Mês inválido: {value!r}; use AAAA-MM.")
    return int(match.group("year")), int(match.group("month"))


def build_identity(year: int, month: int, symbol: str = ALLOWED_SYMBOL, interval: str = ALLOWED_INTERVAL) -> MonthIdentity:
    if symbol not in ALLOWED_SYMBOLS:
        raise IngestionError("SYMBOL_NOT_ALLOWED", f"Símbolo fora da allowlist Spot: {', '.join(ALLOWED_SYMBOLS)}.")
    if interval != ALLOWED_INTERVAL:
        raise IngestionError("INTERVAL_NOT_ALLOWED", f"Somente o intervalo {ALLOWED_INTERVAL} está habilitado em F1.")
    if not 1 <= month <= 12:
        raise IngestionError("INVALID_MONTH", "O mês deve estar entre 01 e 12.")
    current = datetime.now(UTC)
    if (year, month) < MIN_MONTH:
        raise IngestionError("MONTH_NOT_ALLOWED", "F1 aceita meses a partir de 2017-08 para BTCUSDT.")
    if (year, month) >= (current.year, current.month):
        raise IngestionError("MONTH_NOT_CLOSED", "F1 aceita somente meses fechados.")
    return MonthIdentity(symbol=symbol, interval=interval, year=year, month=month)


def build_daily_identity(value: str, symbol: str = ALLOWED_SYMBOL, interval: str = ALLOWED_INTERVAL) -> MonthIdentity:
    if symbol not in ALLOWED_SYMBOLS:
        raise IngestionError("SYMBOL_NOT_ALLOWED", f"Símbolo fora da allowlist Spot: {', '.join(ALLOWED_SYMBOLS)}.")
    if interval != ALLOWED_INTERVAL:
        raise IngestionError("INTERVAL_NOT_ALLOWED", f"Somente o intervalo {ALLOWED_INTERVAL} está habilitado em F5.")
    try:
        day = date.fromisoformat(value)
    except ValueError as error:
        raise IngestionError("INVALID_DATE", f"Data inválida: {value!r}; use AAAA-MM-DD.") from error
    if day.isoformat() != value or (day.year, day.month) < MIN_MONTH:
        raise IngestionError("DATE_NOT_ALLOWED", "A data deve ser ISO e não anterior a 2017-08-01.")
    if day >= datetime.now(UTC).date():
        raise IngestionError("DATE_NOT_CLOSED", "F5 aceita somente dias UTC fechados.")
    return MonthIdentity(symbol=symbol, interval=interval, year=day.year, month=day.month, day=day.day)


def month_range(start: str, end: str, *, symbol: str = ALLOWED_SYMBOL, interval: str = ALLOWED_INTERVAL) -> list[MonthIdentity]:
    start_year, start_month = parse_month(start)
    end_year, end_month = parse_month(end)
    if (end_year, end_month) < (start_year, start_month):
        raise IngestionError("INVALID_RANGE", "--end deve ser igual ou posterior a --start.")
    count = (end_year - start_year) * 12 + end_month - start_month + 1
    if count > MAX_MONTHS_PER_COMMAND:
        raise IngestionError(
            "RANGE_TOO_LARGE",
            f"F1 limita cada comando a {MAX_MONTHS_PER_COMMAND} meses; planeje intervalos menores.",
        )
    return [
        build_identity(
            start_year + (start_month - 1 + offset) // 12,
            (start_month - 1 + offset) % 12 + 1,
            symbol,
            interval,
        )
        for offset in range(count)
    ]


def daily_range(start: str, end: str, *, symbol: str = ALLOWED_SYMBOL, interval: str = ALLOWED_INTERVAL) -> list[MonthIdentity]:
    first = build_daily_identity(start, symbol, interval)
    last = build_daily_identity(end, symbol, interval)
    first_day = date(first.year, first.month, first.day or 1)
    last_day = date(last.year, last.month, last.day or 1)
    if last_day < first_day:
        raise IngestionError("INVALID_RANGE", "--end deve ser igual ou posterior a --start.")
    count = (last_day - first_day).days + 1
    if count > MAX_DAYS_PER_COMMAND:
        raise IngestionError("RANGE_TOO_LARGE", f"F5 limita cada plano diário a {MAX_DAYS_PER_COMMAND} ZIPs.")
    return [
        build_daily_identity((first_day + timedelta(days=offset)).isoformat(), symbol, interval)
        for offset in range(count)
    ]


def archive_url(identity: MonthIdentity) -> str:
    path = (
        f"/data/spot/{identity.granularity}/klines/{identity.symbol}/{identity.interval}/"
        f"{identity.zip_name[:-4]}.zip"
    )
    return f"https://{OFFICIAL_HOST}{path}"


def checksum_url(identity: MonthIdentity) -> str:
    return f"{archive_url(identity)}.CHECKSUM"


def validate_official_url(url: str, identity: MonthIdentity) -> str:
    """Valida cada URL e destino de redirect antes de abrir uma conexão."""
    if identity.day is None:
        build_identity(identity.year, identity.month, identity.symbol, identity.interval)
    else:
        build_daily_identity(
            f"{identity.year:04d}-{identity.month:02d}-{identity.day:02d}",
            identity.symbol,
            identity.interval,
        )
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise IngestionError("HOST_NOT_ALLOWED", "URL oficial inválida.") from error
    expected_zip = archive_url(identity)
    expected_checksum = checksum_url(identity)
    if (
        parts.scheme != "https"
        or parts.hostname != OFFICIAL_HOST
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or parts.query
        or parts.fragment
        or url not in (expected_zip, expected_checksum)
    ):
        raise IngestionError(
            "HOST_OR_FILE_NOT_ALLOWED",
            "URL recusada: use somente o ZIP mensal BTCUSDT Spot 1m e seu CHECKSUM em data.binance.vision.",
        )
    period_pattern = (
        r"(?P<year>\d{4})-(?P<month>0[1-9]|1[0-2])-(?P<day>0[1-9]|[12]\d|3[01])"
        if identity.day is not None
        else r"(?P<year>\d{4})-(?P<month>0[1-9]|1[0-2])"
    )
    pattern = re.compile(
        rf"^/data/spot/{identity.granularity}/klines/{identity.symbol}/{identity.interval}/"
        rf"{re.escape(identity.symbol)}-{re.escape(identity.interval)}-{period_pattern}\.zip(?P<checksum>\.CHECKSUM)?$"
    )
    match = pattern.fullmatch(parts.path)
    if (
        not match
        or (int(match.group("year")), int(match.group("month"))) != (identity.year, identity.month)
        or (identity.day is not None and int(match.group("day")) != identity.day)
        or parts.path.rsplit("/", 1)[-1]
        not in (identity.zip_name, f"{identity.zip_name}.CHECKSUM")
    ):
        raise IngestionError("FILE_NOT_ALLOWED", "Caminho do arquivo fora da allowlist F1.")
    return url


def _db_path(root: Path) -> Path:
    return root / "state" / "quantlab.sqlite"


def _connect_catalog(root: Path) -> sqlite3.Connection:
    database = _db_path(root)
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS raw_objects (
            object_id TEXT PRIMARY KEY,
            identity TEXT NOT NULL,
            symbol TEXT NOT NULL,
            market_type TEXT NOT NULL CHECK (market_type = 'SPOT'),
            interval TEXT NOT NULL,
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            period_start_utc TEXT NOT NULL,
            period_end_utc TEXT NOT NULL,
            source_url TEXT NOT NULL,
            checksum_url TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            byte_size INTEGER NOT NULL CHECK (byte_size > 0),
            etag TEXT,
            checksum_sha256 TEXT,
            checksum_status TEXT NOT NULL CHECK (checksum_status IN ('VERIFIED', 'UNVERIFIED')),
            status TEXT NOT NULL,
            revision INTEGER NOT NULL,
            supersedes_id TEXT REFERENCES raw_objects(object_id),
            acquisition_method TEXT NOT NULL,
            original_zip_name TEXT NOT NULL,
            raw_path TEXT NOT NULL,
            checksum_path TEXT,
            manifest_path TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            UNIQUE(identity, sha256),
            UNIQUE(identity, revision)
        );
        CREATE INDEX IF NOT EXISTS raw_objects_by_identity
            ON raw_objects(identity, revision DESC);
        CREATE TABLE IF NOT EXISTS ingestion_errors (
            error_id TEXT PRIMARY KEY,
            identity TEXT NOT NULL,
            source_url TEXT,
            endpoint_role TEXT,
            error_code TEXT NOT NULL,
            status_code INTEGER,
            attempt INTEGER NOT NULL,
            retry_count INTEGER NOT NULL,
            message TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );
        """
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(raw_objects)")}
    if "granularity" not in columns:
        connection.execute("ALTER TABLE raw_objects ADD COLUMN granularity TEXT NOT NULL DEFAULT 'monthly'")
    if "day" not in columns:
        connection.execute("ALTER TABLE raw_objects ADD COLUMN day INTEGER")
    return connection


def _record_error(
    root: Path,
    identity: MonthIdentity,
    *,
    error_code: str,
    message: str,
    source_url: str | None = None,
    endpoint_role: str | None = None,
    status_code: int | None = None,
    attempt: int = 1,
    retry_count: int = 0,
) -> None:
    with closing(_connect_catalog(root)) as connection, connection:
        connection.execute(
            """
            INSERT INTO ingestion_errors (
                error_id, identity, source_url, endpoint_role, error_code, status_code,
                attempt, retry_count, message, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid4().hex,
                identity.key,
                source_url,
                endpoint_role,
                error_code,
                status_code,
                attempt,
                retry_count,
                message[:1000],
                datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            ),
        )


def _latest_record(root: Path, identity: MonthIdentity) -> sqlite3.Row | None:
    database = _db_path(root)
    if not database.is_file():
        return None
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT * FROM raw_objects WHERE identity = ? ORDER BY revision DESC LIMIT 1",
            (identity.key,),
        ).fetchone()


def _record_for_hash(root: Path, identity: MonthIdentity, sha256: str) -> sqlite3.Row | None:
    database = _db_path(root)
    if not database.is_file():
        return None
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT * FROM raw_objects WHERE identity = ? AND sha256 = ?",
            (identity.key, sha256),
        ).fetchone()


def _read_checksum(checksum_bytes: bytes, identity: MonthIdentity) -> str:
    if len(checksum_bytes) > MAX_CHECKSUM_BYTES:
        raise IngestionError("CHECKSUM_TOO_LARGE", "O arquivo CHECKSUM excede o limite permitido.")
    try:
        text = checksum_bytes.decode("ascii", errors="strict")
    except UnicodeDecodeError as error:
        raise IngestionError("INVALID_CHECKSUM", "CHECKSUM precisa conter texto ASCII no formato SHA-256.") from error
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise IngestionError("INVALID_CHECKSUM", "CHECKSUM deve conter exatamente uma linha não vazia.")
    match = CHECKSUM_PATTERN.fullmatch(lines[0])
    if not match:
        raise IngestionError("INVALID_CHECKSUM", "CHECKSUM inválido; esperado: SHA256  nome-do-zip.zip.")
    filename = match.group("filename")
    if filename is not None and filename != identity.zip_name:
        raise IngestionError(
            "CHECKSUM_FILE_MISMATCH",
            "O nome de arquivo declarado no CHECKSUM não corresponde ao mês solicitado.",
        )
    return match.group("digest").lower()


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            byte_size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), byte_size


def _copy_to_part(source: Path, part_path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_size = 0
    try:
        with source.open("rb") as reader, part_path.open("xb") as writer:
            while chunk := reader.read(1024 * 1024):
                byte_size += len(chunk)
                if byte_size > MAX_ARCHIVE_BYTES:
                    raise IngestionError("ARCHIVE_TOO_LARGE", "O ZIP excede 64 MiB; objeto recusado.")
                writer.write(chunk)
                digest.update(chunk)
            writer.flush()
            os.fsync(writer.fileno())
    except BaseException:
        part_path.unlink(missing_ok=True)
        raise
    if byte_size == 0:
        part_path.unlink(missing_ok=True)
        raise IngestionError("EMPTY_ARCHIVE", "O arquivo ZIP está vazio.")
    return digest.hexdigest(), byte_size


def _validate_zip(path: Path, identity: MonthIdentity) -> ZipValidation:
    try:
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            if len(infos) != 1:
                raise IngestionError(
                    "ZIP_ENTRY_COUNT",
                    f"ZIP recusado: esperado exatamente 1 entrada, recebido {len(infos)}.",
                )
            info = infos[0]
            name = info.filename
            if (
                name != identity.csv_name
                or name in (".", "..")
                or "/" in name
                or "\\" in name
                or "\x00" in name
                or Path(name).name != name
                or info.is_dir()
            ):
                raise IngestionError("ZIP_UNSAFE_ENTRY", f"Entrada ZIP recusada: {name!r}.")
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            if unix_mode and (unix_mode & 0o170000) == 0o120000:
                raise IngestionError("ZIP_UNSAFE_ENTRY", "ZIP com link simbólico não é aceito.")
            if info.flag_bits & 0x1:
                raise IngestionError("ZIP_ENCRYPTED", "ZIP criptografado não é aceito.")
            if info.file_size <= 0 or info.file_size > MAX_UNCOMPRESSED_BYTES:
                raise IngestionError("ZIP_UNCOMPRESSED_TOO_LARGE", "Tamanho descompactado fora do limite F1.")
            if info.compress_size > MAX_ARCHIVE_BYTES:
                raise IngestionError("ARCHIVE_TOO_LARGE", "Tamanho compactado fora do limite F1.")
            if info.compress_size <= 0:
                raise IngestionError("ZIP_INVALID_SIZE", "Tamanho compactado do ZIP inválido.")
            if info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                raise IngestionError("ZIP_COMPRESSION_RATIO", "Razão de compressão excede o limite F1.")
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise IngestionError("ZIP_COMPRESSION_UNSUPPORTED", "Método de compressão ZIP não aceito.")

            actual_bytes = 0
            with archive.open(info, "r") as member:
                while chunk := member.read(1024 * 1024):
                    actual_bytes += len(chunk)
                    if actual_bytes > MAX_UNCOMPRESSED_BYTES:
                        raise IngestionError(
                            "ZIP_UNCOMPRESSED_TOO_LARGE",
                            "Conteúdo descompactado ultrapassou o limite durante a leitura.",
                        )
            if actual_bytes != info.file_size:
                raise IngestionError("ZIP_SIZE_MISMATCH", "Tamanho descompactado diverge do cabeçalho ZIP.")
            return ZipValidation((name,), actual_bytes, True)
    except IngestionError:
        raise
    except (zipfile.BadZipFile, EOFError, RuntimeError, OSError) as error:
        raise IngestionError("ZIP_INVALID_OR_CRC", f"ZIP inválido ou CRC divergente: {error}") from error


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.part")
    try:
        with temp_path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _publish_no_replace(part_path: Path, destination: Path) -> None:
    """Cria o nome final atomicamente sem permitir substituir um objeto existente."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(part_path, destination)
    except FileExistsError as error:
        part_path.unlink(missing_ok=True)
        raise IngestionError("OBJECT_ALREADY_EXISTS", "O destino raw já existe; nenhum arquivo foi sobrescrito.") from error
    except OSError as error:
        part_path.unlink(missing_ok=True)
        raise IngestionError(
            "ATOMIC_PUBLISH_UNAVAILABLE",
            f"Não foi possível publicar o objeto por hard link atômico no mesmo volume: {error}",
        ) from error
    part_path.unlink()


def _quarantine(
    root: Path,
    identity: MonthIdentity,
    part_path: Path,
    *,
    sha256: str,
    byte_size: int,
    reason: str,
) -> None:
    destination_dir = root / "data" / "quarantine" / "binance" / identity.symbol / identity.interval
    period = f"{identity.year:04d}-{identity.month:02d}"
    if identity.day is not None:
        period += f"-{identity.day:02d}"
    destination = destination_dir / f"{period}-{sha256}.zip"
    if not destination.exists():
        _publish_no_replace(part_path, destination)
    manifest = {
        "schema_version": 1,
        "identity": identity.key,
        "sha256": sha256,
        "byte_size": byte_size,
        "status": "QUARANTINED",
        "reason": reason,
        "created_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    _write_json_atomic(destination.with_suffix(".manifest.json"), manifest)


def _object_paths(root: Path, identity: MonthIdentity, sha256: str) -> tuple[Path, Path, Path]:
    directory = root / identity.object_directory / sha256
    zip_path = directory / identity.zip_name
    checksum_path = directory / f"{identity.zip_name}.CHECKSUM"
    manifest_path = directory / "manifest.json"
    return zip_path, checksum_path, manifest_path


def _make_manifest(
    *,
    identity: MonthIdentity,
    object_id: str,
    sha256: str,
    byte_size: int,
    etag: str | None,
    checksum_sha256: str | None,
    checksum_file_sha256: str | None,
    checksum_status: str,
    checksum_origin: str,
    acquisition_method: str,
    revision: int,
    supersedes_id: str | None,
    validation: ZipValidation,
    original_zip_name: str,
    retry_count: int,
) -> dict:
    return {
        "schema_version": 1,
        "object_id": object_id,
        "identity": identity.key,
        "venue": "BINANCE",
        "market_type": "SPOT",
        "symbol": identity.symbol,
        "interval": identity.interval,
        "granularity": identity.granularity,
        "day": identity.day,
        "period_start_utc": identity.period_start_utc,
        "period_end_utc_exclusive": identity.period_end_utc,
        "source_url": archive_url(identity),
        "checksum_url": checksum_url(identity),
        "acquisition_method": acquisition_method,
        "original_zip_name": original_zip_name,
        "sha256": sha256,
        "byte_size": byte_size,
        "etag": etag,
        "checksum_sha256": checksum_sha256,
        "checksum_file_sha256": checksum_file_sha256,
        "checksum_status": checksum_status,
        "source_integrity": checksum_status,
        "status": checksum_status,
        "checksum_origin": checksum_origin,
        "zip_validation": {
            "status": "PASS",
            "entries": list(validation.entries),
            "entry_count": len(validation.entries),
            "uncompressed_bytes": validation.uncompressed_bytes,
            "crc_verified": validation.crc_verified,
        },
        "revision": revision,
        "supersedes_id": supersedes_id,
        "retry_count": retry_count,
        "created_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "downloaded_at_utc": (
            datetime.now(UTC).isoformat().replace("+00:00", "Z")
            if acquisition_method == "remote_fetch"
            else None
        ),
    }


def _commit_valid_object(
    root: Path,
    identity: MonthIdentity,
    part_path: Path,
    *,
    sha256: str,
    byte_size: int,
    checksum_bytes: bytes | None,
    checksum_sha256: str | None,
    checksum_status: str,
    checksum_origin: str,
    acquisition_method: str,
    etag: str | None,
    retry_count: int,
    validation: ZipValidation,
) -> IngestionResult:
    existing = _record_for_hash(root, identity, sha256)
    if existing is not None:
        zip_path = root / existing["raw_path"]
        checksum_path = root / existing["checksum_path"] if existing["checksum_path"] else None
        manifest_path = root / existing["manifest_path"]
        if existing["checksum_status"] == "VERIFIED":
            part_path.unlink(missing_ok=True)
            return IngestionResult(
                "SKIPPED_ALREADY_VERIFIED",
                existing["object_id"],
                sha256,
                int(existing["byte_size"]),
                zip_path,
                manifest_path,
                "VERIFIED",
                int(existing["revision"]),
                retry_count,
            )
        if checksum_status == "UNVERIFIED":
            part_path.unlink(missing_ok=True)
            return IngestionResult(
                "SKIPPED_ALREADY_UNVERIFIED",
                existing["object_id"],
                sha256,
                int(existing["byte_size"]),
                zip_path,
                manifest_path,
                "UNVERIFIED",
                int(existing["revision"]),
                retry_count,
            )
        if checksum_bytes is None:
            raise IngestionError("CHECKSUM_MISSING", "Não é possível promover o objeto sem o checksum validado.")
        checksum_sha = sha256
        checksum_file_sha = hashlib.sha256(checksum_bytes).hexdigest()
        if checksum_path is None:
            _, checksum_path, _ = _object_paths(root, identity, sha256)
            if checksum_path.exists():
                if _read_checksum(checksum_path.read_bytes(), identity) != sha256:
                    raise IngestionError("RAW_CHECKSUM_COLLISION", "O CHECKSUM raw existente diverge; não sobrescrito.")
            else:
                checksum_part = checksum_path.with_name(f".{checksum_path.name}.{uuid4().hex}.part")
                with checksum_part.open("xb") as handle:
                    handle.write(checksum_bytes)
                    handle.flush()
                    os.fsync(handle.fileno())
                _publish_no_replace(checksum_part, checksum_path)
        old_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        old_manifest.update(
            {
                "checksum_sha256": checksum_sha,
                "checksum_file_sha256": checksum_file_sha,
                "checksum_status": "VERIFIED",
                "source_integrity": "VERIFIED",
                "checksum_origin": checksum_origin,
                "verified_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
        )
        _write_json_atomic(manifest_path, old_manifest)
        with closing(_connect_catalog(root)) as connection, connection:
            connection.execute(
                """
                UPDATE raw_objects
                SET checksum_sha256 = ?, checksum_status = 'VERIFIED', status = 'VERIFIED',
                    checksum_path = ?
                WHERE object_id = ?
                """,
                (
                    checksum_sha,
                    checksum_path.relative_to(root).as_posix(),
                    existing["object_id"],
                ),
            )
        part_path.unlink(missing_ok=True)
        return IngestionResult(
            "UPGRADED_TO_VERIFIED",
            existing["object_id"],
            sha256,
            int(existing["byte_size"]),
            zip_path,
            manifest_path,
            "VERIFIED",
            int(existing["revision"]),
            retry_count,
        )

    latest = _latest_record(root, identity)
    revision = 1 if latest is None else int(latest["revision"]) + 1
    supersedes_id = None if latest is None else str(latest["object_id"])
    period_id = f"{identity.year:04d}-{identity.month:02d}"
    if identity.day is not None:
        period_id += f"-{identity.day:02d}"
    object_id = f"binance-spot-{identity.granularity}-{identity.symbol}-{identity.interval}-{period_id}-{sha256}"
    zip_path, checksum_path, manifest_path = _object_paths(root, identity, sha256)
    raw_relative = zip_path.relative_to(root).as_posix()
    checksum_relative = checksum_path.relative_to(root).as_posix() if checksum_bytes is not None else None
    manifest_relative = manifest_path.relative_to(root).as_posix()
    if zip_path.exists():
        existing_sha, existing_size = _hash_file(zip_path)
        if existing_sha != sha256 or existing_size != byte_size:
            raise IngestionError("RAW_COLLISION", "Objeto raw existente diverge do hash esperado; não sobrescrito.")
        part_path.unlink(missing_ok=True)
    else:
        _publish_no_replace(part_path, zip_path)
    try:
        if checksum_bytes is not None:
            if checksum_path.exists():
                if _read_checksum(checksum_path.read_bytes(), identity) != sha256:
                    raise IngestionError(
                        "RAW_CHECKSUM_COLLISION",
                        "O CHECKSUM já armazenado não confere com o objeto; nenhum sidecar foi sobrescrito.",
                    )
            else:
                checksum_part = checksum_path.with_name(f".{checksum_path.name}.{uuid4().hex}.part")
                with checksum_part.open("xb") as handle:
                    handle.write(checksum_bytes)
                    handle.flush()
                    os.fsync(handle.fileno())
                _publish_no_replace(checksum_part, checksum_path)
        manifest = _make_manifest(
            identity=identity,
            object_id=object_id,
            sha256=sha256,
            byte_size=byte_size,
            etag=etag,
            checksum_sha256=checksum_sha256,
            checksum_file_sha256=hashlib.sha256(checksum_bytes).hexdigest() if checksum_bytes is not None else None,
            checksum_status=checksum_status,
            checksum_origin=checksum_origin,
            acquisition_method=acquisition_method,
            revision=revision,
            supersedes_id=supersedes_id,
            validation=validation,
            original_zip_name=identity.zip_name,
            retry_count=retry_count,
        )
        _write_json_atomic(manifest_path, manifest)
        with closing(_connect_catalog(root)) as connection, connection:
            connection.execute(
                """
                INSERT INTO raw_objects (
                    object_id, identity, symbol, market_type, interval, year, month,
                    granularity, day,
                    period_start_utc, period_end_utc, source_url, checksum_url, sha256,
                    byte_size, etag, checksum_sha256, checksum_status, status, revision,
                    supersedes_id, acquisition_method, original_zip_name, raw_path,
                    checksum_path, manifest_path, created_at_utc
                ) VALUES (?, ?, ?, 'SPOT', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    object_id,
                    identity.key,
                    identity.symbol,
                    identity.interval,
                    identity.year,
                    identity.month,
                    identity.granularity,
                    identity.day,
                    identity.period_start_utc,
                    identity.period_end_utc,
                    archive_url(identity),
                    checksum_url(identity),
                    sha256,
                    byte_size,
                    etag,
                    checksum_sha256,
                    checksum_status,
                    checksum_status,
                    revision,
                    supersedes_id,
                    acquisition_method,
                    identity.zip_name,
                    raw_relative,
                    checksum_relative,
                    manifest_relative,
                    datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                ),
            )
    except BaseException:
        # O conteúdo é imutável; em falha de catálogo, mantém o raw já publicado
        # como objeto recuperável e registra o erro na próxima execução.
        raise
    return IngestionResult(
        "IMPORTED_VERIFIED" if checksum_status == "VERIFIED" else "IMPORTED_UNVERIFIED",
        object_id,
        sha256,
        byte_size,
        zip_path,
        manifest_path,
        checksum_status,
        revision,
        retry_count,
    )


def _plan_local_state(root: Path, identity: MonthIdentity) -> tuple[str, int | None]:
    record = _latest_record(root, identity)
    if record is None:
        return "não disponível", None
    raw_path = root / record["raw_path"]
    if not raw_path.is_file():
        return "catálogo sem raw (erro)", int(record["byte_size"])
    return str(record["checksum_status"]), int(record["byte_size"])


def print_plan(
    start: str,
    end: str,
    *,
    root: Path = Path("."),
    symbol: str = ALLOWED_SYMBOL,
    interval: str = ALLOWED_INTERVAL,
    offline: bool = False,
    granularity: str = "monthly",
    max_transfer_bytes: int = DEFAULT_MAX_TRANSFER_BYTES,
) -> None:
    if max_transfer_bytes <= 0:
        raise IngestionError("TRANSFER_LIMIT_EXCEEDED", "O limite agregado de transferência deve ser positivo.")
    if granularity == "monthly":
        identities = month_range(start, end, symbol=symbol, interval=interval)
    elif granularity == "daily":
        identities = daily_range(start, end, symbol=symbol, interval=interval)
    else:
        raise IngestionError("GRANULARITY_NOT_ALLOWED", "Granularidade deve ser monthly ou daily.")
    period_end = datetime.fromisoformat(identities[-1].period_end_utc.replace("Z", "+00:00")).date() - timedelta(days=1)
    print(f"Plano Binance Public Data — Spot {symbol} 1m ({granularity})")
    print(f"Período UTC fechado: {identities[0].period_start_utc[:10]} a {period_end.isoformat()}")
    print(f"Arquivos planejados: {len(identities)} ZIP(s) + {len(identities)} CHECKSUM(s); teto do comando: {max_transfer_bytes:,} bytes")
    if len(identities) > MAX_ARCHIVES_PER_COMMAND:
        raise IngestionError("RANGE_TOO_LARGE", f"F5 limita cada comando a {MAX_ARCHIVES_PER_COMMAND} ZIPs.")
    total_local_bytes = 0
    unknown_size_count = 0
    for identity in identities:
        local_status, byte_size = _plan_local_state(root, identity)
        size_text = (
            f"{byte_size:,} bytes locais"
            if byte_size is not None
            else "desconhecido (sem consulta remota de tamanho)"
        )
        if offline:
            local_text = f"offline: {local_status}"
        else:
            local_text = f"local: {local_status}"
        if byte_size is None:
            unknown_size_count += 1
        else:
            total_local_bytes += byte_size
        print(f"- {identity.zip_name}")
        print(f"  ZIP: {archive_url(identity)}")
        print(f"  CHECKSUM: {checksum_url(identity)}")
        print(f"  arquivos: 2 (1 ZIP + 1 CHECKSUM) | teto/ZIP: {MAX_ARCHIVE_BYTES:,} bytes | tamanho: {size_text} | {local_text}")
    print(f"Tamanho remoto não consultado; {unknown_size_count} ZIP(s) sem tamanho conhecido. Total local conhecido: {total_local_bytes:,} bytes.")
    worst_case = len(identities) * (MAX_ARCHIVE_BYTES + MAX_CHECKSUM_BYTES)
    print(f"Pior caso teórico pelo teto por arquivo: {worst_case:,} bytes; o fetch interrompe antes de exceder o limite agregado.")
    print("O plano não baixa arquivos. Monthly: até 3 meses. Daily: até 31 dias.")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def _open_without_redirects(url: str):
    opener = urllib.request.build_opener(_NoRedirectHandler())
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "QuantLab-F1/0.1"},
        method="GET",
    )
    try:
        return opener.open(request, timeout=30)
    except urllib.error.HTTPError as response:
        # HTTPError é também um response legível; redirects e status são avaliados abaixo.
        return response


def _retry_delay(response, attempt: int) -> float:
    header = None if response is None else response.headers.get("Retry-After")
    if header:
        try:
            return min(max(float(header), 0.0), 2.0)
        except ValueError:
            pass
    return min(0.25 * (2 ** (attempt - 1)), 1.0)


def _stream_url_to_part(
    url: str,
    identity: MonthIdentity,
    destination: Path,
    *,
    role: str,
    maximum_bytes: int,
    root: Path,
    transfer_budget: _TransferBudget | None = None,
) -> tuple[str, int, str | None, bytes | None, int]:
    validate_official_url(url, identity)
    final_error: IngestionError | None = None
    retry_count = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        current_url = url
        seen_redirects: set[str] = set()
        response = None
        try:
            for redirect_number in range(MAX_REDIRECTS + 1):
                validate_official_url(current_url, identity)
                if current_url in seen_redirects:
                    raise IngestionError("REDIRECT_LOOP", "Redirect circular recusado.")
                seen_redirects.add(current_url)
                response = _open_without_redirects(current_url)
                status_code = response.getcode()
                if status_code not in (301, 302, 303, 307, 308):
                    break
                location = response.headers.get("Location")
                response.close()
                if not location or redirect_number == MAX_REDIRECTS:
                    raise IngestionError("REDIRECT_REJECTED", "Redirect ausente ou acima do limite F1.")
                redirected_url = urljoin(current_url, location)
                validate_official_url(redirected_url, identity)
                current_url = redirected_url

            assert response is not None
            status_code = response.getcode()
            if status_code == 404:
                raise IngestionError(
                    "MISSING_NOT_CONFIRMED",
                    f"HTTP 404 para {role}; arquivo ausente ou ainda não publicado (não representa zero negociações).",
                    status_code=404,
                    retry_count=retry_count,
                )
            if status_code == 429 or status_code >= 500:
                status = status_code
                if attempt < MAX_ATTEMPTS:
                    delay = _retry_delay(response, attempt)
                    response.close()
                    retry_count += 1
                    _record_error(
                        root,
                        identity,
                        error_code="HTTP_RETRY",
                        message=f"HTTP {status} em {role}; nova tentativa limitada.",
                        source_url=url,
                        endpoint_role=role,
                        status_code=status,
                        attempt=attempt,
                        retry_count=retry_count,
                    )
                    time.sleep(delay)
                    continue
                raise IngestionError(
                    "RATE_LIMIT" if status == 429 else "HTTP_SERVER_ERROR",
                    f"HTTP {status} em {role} após {attempt} tentativa(s).",
                    status_code=status,
                    retry_count=retry_count,
                )
            if status_code < 200 or status_code >= 300:
                raise IngestionError(
                    "HTTP_ERROR",
                    f"HTTP {status_code} em {role}.",
                    status_code=status_code,
                    retry_count=retry_count,
                )
            content_length = response.headers.get("Content-Length")
            declared_length: int | None = None
            if content_length:
                try:
                    declared_length = int(content_length)
                    if declared_length > maximum_bytes:
                        raise IngestionError("REMOTE_TOO_LARGE", f"Content-Length excede limite para {role}.")
                    if transfer_budget is not None and declared_length > transfer_budget.remaining_bytes:
                        raise IngestionError("TRANSFER_LIMIT_EXCEEDED", f"Content-Length de {role} excede o saldo agregado.")
                except ValueError:
                    raise IngestionError("INVALID_CONTENT_LENGTH", f"Content-Length inválido em {role}.")

            digest = hashlib.sha256()
            byte_size = 0
            collected = bytearray() if role == "checksum" else None
            etag = response.headers.get("ETag")
            try:
                with destination.open("xb") as handle:
                    while True:
                        per_response_remaining = maximum_bytes - byte_size
                        aggregate_remaining = (
                            transfer_budget.remaining_bytes if transfer_budget is not None else per_response_remaining
                        )
                        read_size = min(1024 * 1024, per_response_remaining, aggregate_remaining)
                        if read_size <= 0:
                            if declared_length is not None and byte_size == declared_length:
                                break
                            code = "REMOTE_TOO_LARGE" if per_response_remaining <= 0 else "TRANSFER_LIMIT_EXCEEDED"
                            raise IngestionError(code, f"Download de {role} atingiu seu limite antes de confirmar o fim do arquivo.")
                        chunk = response.read(read_size)
                        if not chunk:
                            break
                        byte_size += len(chunk)
                        if transfer_budget is not None:
                            transfer_budget.transferred_bytes += len(chunk)
                        try:
                            handle.write(chunk)
                        except OSError as error:
                            raise IngestionError("DISK_WRITE_ERROR", f"Falha ao gravar {role}: {error}") from error
                        digest.update(chunk)
                        if collected is not None:
                            collected.extend(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
            if byte_size == 0:
                destination.unlink(missing_ok=True)
                raise IngestionError("EMPTY_REMOTE_FILE", f"Resposta vazia para {role}.")
            return digest.hexdigest(), byte_size, etag, bytes(collected) if collected is not None else None, retry_count
        except IngestionError as error:
            if response is not None:
                response.close()
            destination.unlink(missing_ok=True)
            final_error = error
            if error.code in ("MISSING_NOT_CONFIRMED", "HOST_OR_FILE_NOT_ALLOWED", "FILE_NOT_ALLOWED", "REDIRECT_REJECTED", "REDIRECT_LOOP"):
                raise
            if error.status_code == 429 and attempt < MAX_ATTEMPTS:
                retry_count += 1
                time.sleep(0.25 * attempt)
                continue
            if error.code in ("HTTP_SERVER_ERROR", "RATE_LIMIT"):
                raise
            raise
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError) as error:
            if response is not None:
                response.close()
            destination.unlink(missing_ok=True)
            reason = getattr(error, "reason", error)
            final_error = IngestionError("NETWORK_ERROR", f"Falha de rede em {role}: {reason}")
            if attempt < MAX_ATTEMPTS:
                retry_count += 1
                _record_error(
                    root,
                    identity,
                    error_code="NETWORK_RETRY",
                    message=f"Falha de rede em {role}; nova tentativa limitada.",
                    source_url=url,
                    endpoint_role=role,
                    attempt=attempt,
                    retry_count=retry_count,
                )
                time.sleep(_retry_delay(None, attempt))
                continue
        finally:
            if response is not None:
                response.close()
        if final_error is not None:
            final_error.retry_count = retry_count
    if final_error is not None:
        raise final_error
    raise IngestionError("FETCH_FAILED", f"Falha não classificada em {role}.", retry_count=retry_count)


def fetch_month(
    identity: MonthIdentity,
    *,
    root: Path = Path("."),
    refresh: bool = False,
    max_transfer_bytes: int = DEFAULT_MAX_TRANSFER_BYTES,
) -> IngestionResult:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    existing = _latest_record(root, identity)
    if existing is not None and existing["checksum_status"] == "VERIFIED" and not refresh:
        raw_path = root / existing["raw_path"]
        if raw_path.is_file():
            print(f"SKIPPED_ALREADY_VERIFIED {identity.key} sha256={existing['sha256']}")
            return IngestionResult(
                "SKIPPED_ALREADY_VERIFIED",
                existing["object_id"],
                existing["sha256"],
                int(existing["byte_size"]),
                raw_path,
                root / existing["manifest_path"],
                "VERIFIED",
                int(existing["revision"]),
            )
    if max_transfer_bytes <= 0:
        raise IngestionError("TRANSFER_LIMIT_EXCEEDED", "Não resta limite de transferência para este ZIP.")
    raw_root = root / "data" / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)
    incoming_dir = Path(tempfile.mkdtemp(prefix=".incoming-", dir=raw_root))
    checksum_part = incoming_dir / f"{identity.zip_name}.CHECKSUM.part"
    zip_part = incoming_dir / f"{identity.zip_name}.part"
    retry_count = 0
    transfer_budget = _TransferBudget(max_transfer_bytes)
    active_url = checksum_url(identity)
    active_role = "checksum"
    try:
        _, _, _, checksum_bytes, checksum_retries = _stream_url_to_part(
            active_url,
            identity,
            checksum_part,
            role="checksum",
            maximum_bytes=MAX_CHECKSUM_BYTES,
            root=root,
            transfer_budget=transfer_budget,
        )
        retry_count += checksum_retries
        if checksum_bytes is None:
            raise IngestionError("EMPTY_CHECKSUM", "O endpoint oficial retornou CHECKSUM vazio.")
        expected_sha = _read_checksum(checksum_bytes, identity)
        active_url = archive_url(identity)
        active_role = "zip"
        zip_digest, byte_size, etag, _, zip_retries = _stream_url_to_part(
            active_url,
            identity,
            zip_part,
            role="zip",
            maximum_bytes=MAX_ARCHIVE_BYTES,
            root=root,
            transfer_budget=transfer_budget,
        )
        retry_count += zip_retries
        if zip_digest != expected_sha:
            _quarantine(
                root,
                identity,
                zip_part,
                sha256=zip_digest,
                byte_size=byte_size,
                reason="CHECKSUM_MISMATCH",
            )
            raise IngestionError(
                "CHECKSUM_MISMATCH",
                f"SHA-256 local {zip_digest} diverge do checksum publicado {expected_sha}; ZIP em quarentena.",
                retry_count=retry_count,
            )
        validation = _validate_zip(zip_part, identity)
        result = _commit_valid_object(
            root,
            identity,
            zip_part,
            sha256=zip_digest,
            byte_size=byte_size,
            checksum_bytes=checksum_bytes,
            checksum_sha256=expected_sha,
            checksum_status="VERIFIED",
            checksum_origin="binance_public_data_download",
            acquisition_method="remote_fetch",
            etag=etag,
            retry_count=retry_count,
            validation=validation,
        )
        result = IngestionResult(
            result.status, result.object_id, result.sha256, result.byte_size, result.raw_path,
            result.manifest_path, result.integrity_status, result.revision, result.retry_count,
            transfer_budget.transferred_bytes,
        )
        print(f"{result.status} {identity.key} sha256={result.sha256}")
        return result
    except (IngestionError, OSError, urllib.error.URLError, http.client.HTTPException) as error:
        if isinstance(error, IngestionError):
            error.transferred_bytes = transfer_budget.transferred_bytes
        if zip_part.is_file():
            digest, size = _hash_file(zip_part)
            _quarantine(root, identity, zip_part, sha256=digest, byte_size=size, reason=str(error))
        code = error.code if isinstance(error, IngestionError) else "IO_OR_HTTP_ERROR"
        status_code = error.status_code if isinstance(error, IngestionError) else None
        retries = error.retry_count if isinstance(error, IngestionError) else retry_count
        _record_error(
            root,
            identity,
            error_code=code,
            message=str(error),
            status_code=status_code,
            attempt=retry_count + 1,
            retry_count=retries,
            source_url=active_url,
            endpoint_role=active_role,
        )
        raise
    finally:
        shutil.rmtree(incoming_dir, ignore_errors=True)


def import_archive(
    zip_path: Path,
    checksum_path: Path | None = None,
    *,
    root: Path = Path("."),
) -> IngestionResult:
    root = root.resolve()
    identity: MonthIdentity | None = None
    part_path: Path | None = None
    try:
        filename = zip_path.name
        match = re.fullmatch(r"(BTCUSDT|ETHUSDT)-1m-(\d{4})-(0[1-9]|1[0-2])(?:-(0[1-9]|[12]\d|3[01]))?\.zip", filename)
        if not match:
            raise IngestionError(
                "IMPORT_FILENAME_NOT_ALLOWED",
                "O ZIP deve manter o nome oficial SYMBOL-1m-AAAA-MM.zip ou SYMBOL-1m-AAAA-MM-DD.zip.",
            )
        if match.group(4):
            identity = build_daily_identity(f"{match.group(2)}-{match.group(3)}-{match.group(4)}", match.group(1))
        else:
            identity = build_identity(int(match.group(2)), int(match.group(3)), match.group(1))
        if not zip_path.is_file():
            raise IngestionError("ZIP_NOT_FOUND", f"ZIP não encontrado: {zip_path}.")
        if checksum_path is not None and not checksum_path.is_file():
            raise IngestionError("CHECKSUM_NOT_FOUND", f"CHECKSUM não encontrado: {checksum_path}.")
        incoming_root = root / "data" / "raw"
        incoming_root.mkdir(parents=True, exist_ok=True)
        incoming_dir = Path(tempfile.mkdtemp(prefix=".incoming-", dir=incoming_root))
        part_path = incoming_dir / f"{identity.zip_name}.part"
        sha256, byte_size = _copy_to_part(zip_path, part_path)
        checksum_bytes: bytes | None = None
        expected_sha: str | None = None
        checksum_origin = "none"
        checksum_status = "UNVERIFIED"
        if checksum_path is not None:
            if checksum_path.stat().st_size > MAX_CHECKSUM_BYTES:
                error = IngestionError("CHECKSUM_TOO_LARGE", "O arquivo CHECKSUM excede o limite permitido.")
                _quarantine(root, identity, part_path, sha256=sha256, byte_size=byte_size, reason=error.code)
                raise error
            checksum_bytes = checksum_path.read_bytes()
            try:
                expected_sha = _read_checksum(checksum_bytes, identity)
            except IngestionError as error:
                _quarantine(root, identity, part_path, sha256=sha256, byte_size=byte_size, reason=error.code)
                raise
            checksum_origin = "imported_checksum_path"
            if sha256 != expected_sha:
                _quarantine(
                    root,
                    identity,
                    part_path,
                    sha256=sha256,
                    byte_size=byte_size,
                    reason="CHECKSUM_MISMATCH",
                )
                raise IngestionError(
                    "CHECKSUM_MISMATCH",
                    f"SHA-256 local {sha256} diverge do checksum fornecido {expected_sha}; cópia rejeitada em quarentena.",
                )
            checksum_status = "VERIFIED"
        try:
            validation = _validate_zip(part_path, identity)
        except IngestionError as error:
            _quarantine(root, identity, part_path, sha256=sha256, byte_size=byte_size, reason=error.code)
            raise
        result = _commit_valid_object(
            root,
            identity,
            part_path,
            sha256=sha256,
            byte_size=byte_size,
            checksum_bytes=checksum_bytes,
            checksum_sha256=expected_sha,
            checksum_status=checksum_status,
            checksum_origin=checksum_origin,
            acquisition_method="offline_import",
            etag=None,
            retry_count=0,
            validation=validation,
        )
        print(f"{result.status} {identity.key} sha256={result.sha256}")
        if result.integrity_status == "UNVERIFIED":
            print("INTEGRIDADE DA FONTE: UNVERIFIED — ZIP validado por CRC, sem checksum publicado fornecido.")
        return result
    except (IngestionError, OSError, zipfile.BadZipFile) as error:
        if identity is not None:
            code = error.code if isinstance(error, IngestionError) else "IO_OR_ZIP_ERROR"
            _record_error(
                root,
                identity,
                error_code=code,
                message=str(error),
                source_url=archive_url(identity),
                endpoint_role="offline_import",
            )
        if part_path is not None:
            part_path.unlink(missing_ok=True)
        raise
    finally:
        if part_path is not None:
            shutil.rmtree(part_path.parent, ignore_errors=True)

