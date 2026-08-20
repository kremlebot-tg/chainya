from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

OPS = Path(__file__).parents[2] / "ops"
OFFSITE_SERVICE = OPS / "chainya-offsite-backup.service"
OFFSITE_TIMER = OPS / "chainya-offsite-backup.timer"
sys.path.insert(0, str(OPS))

import recovery_bundle
import verify_chainya_backups


def make_backups(root: Path, *, now: datetime) -> Path:
    root.mkdir(mode=0o700)
    stamp = now.strftime("%Y-%m-%dT%H%M%SZ")
    database = root / f"orders-{stamp}.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE orders (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO orders VALUES ('safe-test-order')")
    database.chmod(0o600)
    os.utime(database, (now.timestamp(), now.timestamp()))

    catalog = root / f"catalog-{stamp}.tar.gz"
    payloads = {
        "catalog.json": json.dumps({"teas": [{"id": "tea"}]}).encode(),
        "catalog-media/" + "a" * 32 + ".webp": b"test-webp",
    }
    with tarfile.open(catalog, "w:gz") as archive:
        for name, payload in payloads.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(payload))
    catalog.chmod(0o600)
    os.utime(catalog, (now.timestamp(), now.timestamp()))
    return root


def make_certificate(root: Path) -> tuple[Path, Path]:
    private_key = root / "private.pem"
    certificate = root / "recipient.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-sha256",
            "-days",
            "1",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
            "-subj",
            "/CN=Chainya-Recovery-Test",
        ],
        check=True,
        capture_output=True,
    )
    private_key.chmod(0o600)
    certificate.chmod(0o600)
    return private_key, certificate


def test_backup_verifier_checks_database_catalog_and_media(tmp_path):
    now = datetime(2026, 8, 21, 10, tzinfo=timezone.utc)
    backup_directory = make_backups(tmp_path / "backups", now=now)

    result = verify_chainya_backups.verify_backup_directory(
        backup_directory, max_age_hours=36, now=now + timedelta(hours=2)
    )

    assert result == {
        "status": "ok",
        "database_age_hours": 2.0,
        "catalog_age_hours": 2.0,
        "catalog_items": 1,
        "catalog_media_files": 1,
    }


def test_backup_verifier_rejects_stale_or_overexposed_copy(tmp_path):
    now = datetime(2026, 8, 21, 10, tzinfo=timezone.utc)
    backup_directory = make_backups(tmp_path / "backups", now=now)
    with pytest.raises(
        verify_chainya_backups.BackupVerificationError,
        match="stale",
    ):
        verify_chainya_backups.verify_backup_directory(
            backup_directory, max_age_hours=36, now=now + timedelta(hours=37)
        )

    database = next(backup_directory.glob("orders-*.sqlite3"))
    database.chmod(0o644)
    with pytest.raises(
        verify_chainya_backups.BackupVerificationError,
        match="permissions",
    ):
        verify_chainya_backups.verify_backup_directory(
            backup_directory, max_age_hours=36, now=now
        )


def test_catalog_verifier_rejects_path_traversal(tmp_path):
    archive_path = tmp_path / "catalog.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        payload = b"bad"
        info = tarfile.TarInfo("../catalog.json")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    archive_path.chmod(0o600)

    with pytest.raises(
        verify_chainya_backups.BackupVerificationError,
        match="unsafe path",
    ):
        verify_chainya_backups.verify_catalog_archive(archive_path)


def test_encrypted_recovery_bundle_round_trip(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    backup_directory = make_backups(tmp_path / "backups", now=now)
    private_key, certificate = make_certificate(tmp_path)
    shop_env = tmp_path / "chainya-shop.env"
    shop_env.write_text("CHAINYA_TEST_MODE=0\n", encoding="utf-8")
    shop_env.chmod(0o600)
    marker = tmp_path / "web-release-commit"
    marker.write_text("9" * 40 + "\n", encoding="ascii")
    marker.chmod(0o600)

    bundle = recovery_bundle.create_bundle(
        backup_directory=backup_directory,
        recipient_certificate=certificate,
        output_directory=tmp_path / "encrypted",
        required_paths=(shop_env, marker),
        optional_paths=(),
        now=now,
    )

    assert bundle.is_file()
    assert bundle.stat().st_mode & 0o777 == 0o600
    extracted = tmp_path / "extracted"
    result = recovery_bundle.verify_bundle(
        bundle=bundle,
        recipient_certificate=certificate,
        private_key=private_key,
        extract_directory=extracted,
    )
    assert result == {
        "status": "ok",
        "release_commit": "9" * 40,
        "files": 4,
        "catalog_items": 1,
        "catalog_media_files": 1,
    }
    assert (extracted / "data/orders.sqlite3").is_file()
    assert (extracted / "data/catalog.tar.gz").is_file()
    assert (extracted / "config/chainya-shop.env").read_text() == (
        "CHAINYA_TEST_MODE=0\n"
    )
    assert (extracted / "manifest.json").stat().st_mode & 0o777 == 0o600


def test_recovery_bundle_rejects_invalid_destination(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    backup_directory = make_backups(tmp_path / "backups", now=now)
    _, certificate = make_certificate(tmp_path)
    marker = tmp_path / "web-release-commit"
    marker.write_text("a" * 40 + "\n", encoding="ascii")
    marker.chmod(0o600)

    with pytest.raises(recovery_bundle.RecoveryBundleError, match="destination"):
        recovery_bundle.create_bundle(
            backup_directory=backup_directory,
            recipient_certificate=certificate,
            output_directory=tmp_path / "encrypted",
            required_paths=(marker,),
            optional_paths=(),
            destination="-e dangerous",
            now=now,
        )


def test_offsite_units_remain_fail_closed_and_unprivileged_on_destination():
    service = OFFSITE_SERVICE.read_text(encoding="utf-8")
    timer = OFFSITE_TIMER.read_text(encoding="utf-8")

    assert "EnvironmentFile=/etc/chainya-offsite-backup.env" in service
    assert "verify_chainya_backups.py" in service
    assert "recovery_bundle.py create" in service
    assert "CHAINYA_OFFSITE_RECIPIENT_CERT" in service
    assert "CHAINYA_OFFSITE_SSH_IDENTITY" in service
    assert "CHAINYA_OFFSITE_KNOWN_HOSTS" in service
    assert "NoNewPrivileges=true" in service
    assert "ProtectSystem=strict" in service
    assert "ProtectHome=true" in service
    assert "CapabilityBoundingSet=CAP_DAC_READ_SEARCH" in service
    assert "ReadWritePaths=/var/backups/chainya-shop/offsite-out" in service
    assert "CHAINYA_OFFSITE_PRIVATE" not in service
    assert "--private-key" not in service
    assert "OnCalendar=*-*-* 04:10:00 Europe/Moscow" in timer
    assert "Persistent=true" in timer
