"""Verificação focal de segurança, checksum e idempotência da F1."""

import hashlib
import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

from quantlab.data_ingestion import (
    IngestionError,
    archive_url,
    build_daily_identity,
    build_identity,
    checksum_url,
    import_archive,
    validate_official_url,
)


class IngestionIntegrityTest(unittest.TestCase):
    def test_official_daily_urls_accept_a_closed_day_in_the_current_month(self) -> None:
        today = datetime.now(UTC).date()
        if today.day == 1:
            self.skipTest("não há dia fechado do mês corrente no primeiro dia UTC")

        identity = build_daily_identity(
            date(today.year, today.month, 1).isoformat(),
            symbol="BTCUSDT",
        )

        self.assertEqual(
            validate_official_url(archive_url(identity), identity),
            archive_url(identity),
        )
        self.assertEqual(
            validate_official_url(checksum_url(identity), identity),
            checksum_url(identity),
        )

    def test_offline_validation_rejects_unsafe_inputs_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "project"
            source = base / "source"
            source.mkdir()
            identity = build_identity(2024, 1)
            archive = source / identity.zip_name
            member_bytes = b"synthetic kline fixture; not exchange data\n"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
                handle.writestr(identity.csv_name, member_bytes)
            original_bytes = archive.read_bytes()
            digest = hashlib.sha256(original_bytes).hexdigest()
            checksum = source / f"{identity.zip_name}.CHECKSUM"
            checksum.write_text(f"{digest}  {identity.zip_name}\n", encoding="ascii")

            first = import_archive(archive, checksum, root=root)
            self.assertEqual(first.status, "IMPORTED_VERIFIED")
            self.assertEqual(first.integrity_status, "VERIFIED")
            self.assertEqual(first.raw_path.read_bytes(), original_bytes)
            manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["source_integrity"], "VERIFIED")
            self.assertEqual(manifest["checksum_sha256"], digest)
            self.assertEqual(
                manifest["checksum_file_sha256"],
                hashlib.sha256(checksum.read_bytes()).hexdigest(),
            )
            self.assertTrue(manifest["zip_validation"]["crc_verified"])
            self.assertEqual(manifest["zip_validation"]["entries"], [identity.csv_name])

            second = import_archive(archive, checksum, root=root)
            self.assertEqual(second.status, "SKIPPED_ALREADY_VERIFIED")
            self.assertEqual(second.object_id, first.object_id)
            with closing(sqlite3.connect(root / "state" / "quantlab.sqlite")) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM raw_objects").fetchone()[0], 1)

            corrupt_dir = source / "corrupt-copy"
            corrupt_dir.mkdir()
            corrupted = corrupt_dir / identity.zip_name
            changed = bytearray(original_bytes)
            changed[len(changed) // 2] ^= 0x01
            corrupted.write_bytes(changed)
            with self.assertRaises(IngestionError) as mismatch:
                import_archive(corrupted, checksum, root=root)
            self.assertEqual(mismatch.exception.code, "CHECKSUM_MISMATCH")
            self.assertEqual(first.raw_path.read_bytes(), original_bytes)

            slip_archive = source / "BTCUSDT-1m-2024-02.zip"
            slip_identity = build_identity(2024, 2)
            with zipfile.ZipFile(slip_archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
                handle.writestr("../escape.csv", b"must not be extracted")
            slip_digest = hashlib.sha256(slip_archive.read_bytes()).hexdigest()
            slip_checksum = source / f"{slip_identity.zip_name}.CHECKSUM"
            slip_checksum.write_text(f"{slip_digest}  {slip_identity.zip_name}\n", encoding="ascii")
            with self.assertRaises(IngestionError) as unsafe_entry:
                import_archive(slip_archive, slip_checksum, root=root)
            self.assertEqual(unsafe_entry.exception.code, "ZIP_UNSAFE_ENTRY")
            self.assertFalse((root / "data" / "escape.csv").exists())

            with self.assertRaises(IngestionError) as unsafe_host:
                validate_official_url(
                    "https://attacker.example/data/spot/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2024-01.zip",
                    identity,
                )
            self.assertEqual(unsafe_host.exception.code, "HOST_OR_FILE_NOT_ALLOWED")
            with self.assertRaises(IngestionError) as unsafe_file:
                validate_official_url(
                    "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2024-01-evil.zip",
                    identity,
                )
            self.assertEqual(unsafe_file.exception.code, "HOST_OR_FILE_NOT_ALLOWED")
            with self.assertRaises(IngestionError) as unsafe_interval:
                build_identity(2024, 1, interval="1h")
            self.assertEqual(unsafe_interval.exception.code, "INTERVAL_NOT_ALLOWED")

            unverified_archive = source / "BTCUSDT-1m-2024-03.zip"
            unverified_identity = build_identity(2024, 3)
            with zipfile.ZipFile(unverified_archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
                handle.writestr(unverified_identity.csv_name, member_bytes)
            unverified = import_archive(unverified_archive, root=root)
            self.assertEqual(unverified.status, "IMPORTED_UNVERIFIED")
            self.assertEqual(unverified.integrity_status, "UNVERIFIED")
            unverified_manifest = json.loads(unverified.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(unverified_manifest["source_integrity"], "UNVERIFIED")


if __name__ == "__main__":
    unittest.main()
