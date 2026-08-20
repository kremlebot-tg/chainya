#!/usr/bin/env python3
"""Create and verify encrypted, off-site Chainya recovery bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from verify_chainya_backups import (
    BackupVerificationError,
    verify_backup_directory,
    verify_catalog_archive,
    verify_sqlite_backup,
)

DEFAULT_REQUIRED_PATHS = (
    Path("/etc/chainya-shop.env"),
    Path("/etc/chainya-shop-admin.env"),
    Path("/etc/chainya-shop-saby.env"),
    Path("/etc/chainya-shop-integrations.env"),
    Path("/etc/chainya-bot.env"),
    Path("/var/lib/chainya-shop/web-release-commit"),
)
DEFAULT_OPTIONAL_PATHS = (Path("/var/lib/chainya-bot/favs.json"),)
DESTINATION_RE = re.compile(
    r"^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:/[A-Za-z0-9._/-]+/$"
)
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class RecoveryBundleError(RuntimeError):
    """Raised when an encrypted recovery bundle is unsafe or incomplete."""


def _run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(command, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RecoveryBundleError("recovery cryptographic operation failed") from exc


def _secure_source(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise RecoveryBundleError("required recovery source is missing")


def _latest(directory: Path, pattern: str) -> Path:
    files = sorted(directory.glob(pattern), key=lambda item: item.stat().st_mtime)
    if not files:
        raise RecoveryBundleError("verified backup source is missing")
    return files[-1]


def _certificate_sha256(certificate: Path, openssl: str) -> str:
    result = _run([openssl, "x509", "-in", str(certificate), "-outform", "DER"])
    return hashlib.sha256(result.stdout).hexdigest()


def _tar_info(name: str, size: int, timestamp: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = 0o600
    info.mtime = timestamp
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def _archive_name(path: Path, *, required: bool) -> str:
    if path.name == "web-release-commit":
        return "state/web-release-commit"
    prefix = "config" if required else "state"
    return f"{prefix}/{path.name}"


def _add_bytes(
    archive: tarfile.TarFile, *, name: str, payload: bytes, timestamp: int
) -> dict[str, int | str]:
    import io

    archive.addfile(_tar_info(name, len(payload), timestamp), io.BytesIO(payload))
    return {
        "name": name,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def create_bundle(
    *,
    backup_directory: Path,
    recipient_certificate: Path,
    output_directory: Path,
    required_paths: tuple[Path, ...] = DEFAULT_REQUIRED_PATHS,
    optional_paths: tuple[Path, ...] = DEFAULT_OPTIONAL_PATHS,
    destination: str | None = None,
    ssh_identity: Path | None = None,
    known_hosts: Path | None = None,
    max_age_hours: float = 36,
    now: datetime | None = None,
    openssl: str = "openssl",
    rsync: str = "rsync",
) -> Path:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(timezone.utc)
    verify_backup_directory(
        backup_directory, max_age_hours=max_age_hours, now=current
    )
    _secure_source(recipient_certificate)
    for path in required_paths:
        _secure_source(path)
    for path in optional_paths:
        if path.exists():
            _secure_source(path)

    if destination and not DESTINATION_RE.fullmatch(destination):
        raise RecoveryBundleError("off-site destination is invalid")
    if ssh_identity is not None:
        _secure_source(ssh_identity)
        if not ssh_identity.is_absolute():
            raise RecoveryBundleError("SSH identity path must be absolute")
    if destination:
        if ssh_identity is None or known_hosts is None:
            raise RecoveryBundleError(
                "off-site upload requires an SSH identity and known_hosts"
            )
        _secure_source(known_hosts)
        if not known_hosts.is_absolute():
            raise RecoveryBundleError("known_hosts path must be absolute")

    release_path = next(
        (path for path in required_paths if path.name == "web-release-commit"), None
    )
    if release_path is None:
        raise RecoveryBundleError("release marker is required")
    release_commit = release_path.read_text(encoding="ascii").strip()
    if not COMMIT_RE.fullmatch(release_commit):
        raise RecoveryBundleError("release marker is invalid")

    output_directory.mkdir(parents=True, exist_ok=True)
    output_directory.chmod(0o700)
    stamp = current.strftime("%Y-%m-%dT%H%M%SZ")
    output = output_directory / f"chainya-recovery-{stamp}-{release_commit[:12]}.cms"
    if output.exists() or output.is_symlink():
        raise RecoveryBundleError("recovery bundle already exists")

    database = _latest(backup_directory, "orders-*.sqlite3")
    catalog = _latest(backup_directory, "catalog-*.tar.gz")
    sources: list[tuple[str, Path]] = [
        ("data/orders.sqlite3", database),
        ("data/catalog.tar.gz", catalog),
    ]
    sources.extend((_archive_name(path, required=True), path) for path in required_paths)
    sources.extend(
        (_archive_name(path, required=False), path)
        for path in optional_paths
        if path.exists()
    )
    archive_names = [name for name, _ in sources]
    if len(archive_names) != len(set(archive_names)):
        raise RecoveryBundleError("duplicate recovery archive name")

    timestamp = int(current.timestamp())
    certificate_hash = _certificate_sha256(recipient_certificate, openssl)
    with tempfile.TemporaryDirectory(prefix=".recovery-", dir=output_directory) as tmp:
        temporary = Path(tmp)
        plaintext = temporary / "bundle.tar.gz"
        encrypted = temporary / "bundle.cms"
        manifest_files: list[dict[str, int | str]] = []
        with tarfile.open(plaintext, "w:gz") as archive:
            for name, path in sources:
                payload = path.read_bytes()
                manifest_files.append(
                    _add_bytes(
                        archive, name=name, payload=payload, timestamp=timestamp
                    )
                )
            manifest = json.dumps(
                {
                    "schema": 1,
                    "created_at": current.isoformat(),
                    "release_commit": release_commit,
                    "recipient_certificate_sha256": certificate_hash,
                    "files": manifest_files,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            _add_bytes(
                archive,
                name="manifest.json",
                payload=manifest,
                timestamp=timestamp,
            )
        plaintext.chmod(0o600)
        _run(
            [
                openssl,
                "cms",
                "-encrypt",
                "-binary",
                "-outform",
                "DER",
                "-aes-256-cbc",
                "-in",
                str(plaintext),
                "-out",
                str(encrypted),
                str(recipient_certificate),
            ]
        )
        _run(
            [openssl, "cms", "-cmsout", "-inform", "DER", "-in", str(encrypted)]
        )
        encrypted.chmod(0o600)
        os.replace(encrypted, output)
    output.chmod(0o600)

    if destination:
        remote = destination + output.name
        command = [rsync, "--archive", "--chmod=F600"]
        if ssh_identity is not None:
            ssh_command = (
                "ssh -F /dev/null -o BatchMode=yes "
                "-o StrictHostKeyChecking=yes "
                f"-o UserKnownHostsFile={shlex.quote(str(known_hosts))} "
                f"-i {shlex.quote(str(ssh_identity))}"
            )
            command.extend(
                [
                    "--rsh",
                    ssh_command,
                ]
            )
        command.extend([str(output), remote])
        _run(command)
    return output


def _safe_bundle_member(member: tarfile.TarInfo) -> PurePosixPath:
    name = PurePosixPath(member.name)
    if name.is_absolute() or ".." in name.parts:
        raise RecoveryBundleError("bundle contains an unsafe path")
    if member.issym() or member.islnk() or not member.isfile():
        raise RecoveryBundleError("bundle contains an unsupported member")
    return name


def verify_bundle(
    *,
    bundle: Path,
    recipient_certificate: Path,
    private_key: Path,
    extract_directory: Path | None = None,
    openssl: str = "openssl",
) -> dict[str, int | str]:
    for path in (bundle, recipient_certificate, private_key):
        _secure_source(path)
    with tempfile.TemporaryDirectory(prefix="chainya-recovery-verify-") as tmp:
        temporary = Path(tmp)
        plaintext = temporary / "bundle.tar.gz"
        _run(
            [
                openssl,
                "cms",
                "-decrypt",
                "-binary",
                "-inform",
                "DER",
                "-in",
                str(bundle),
                "-recip",
                str(recipient_certificate),
                "-inkey",
                str(private_key),
                "-out",
                str(plaintext),
            ]
        )
        payloads: dict[str, bytes] = {}
        try:
            with tarfile.open(plaintext, "r:gz") as archive:
                for member in archive.getmembers():
                    name = str(_safe_bundle_member(member))
                    if name in payloads:
                        raise RecoveryBundleError("bundle contains a duplicate member")
                    source = archive.extractfile(member)
                    if source is None:
                        raise RecoveryBundleError("bundle member cannot be read")
                    payloads[name] = source.read()
        except tarfile.TarError as exc:
            raise RecoveryBundleError("decrypted bundle is not a valid archive") from exc

        try:
            manifest = json.loads(payloads.pop("manifest.json"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecoveryBundleError("bundle manifest is missing or invalid") from exc
        if not isinstance(manifest, dict) or manifest.get("schema") != 1:
            raise RecoveryBundleError("bundle manifest schema is unsupported")
        expected = manifest.get("files")
        if not isinstance(expected, list):
            raise RecoveryBundleError("bundle manifest file list is invalid")
        expected_names: set[str] = set()
        for row in expected:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                raise RecoveryBundleError("bundle manifest entry is invalid")
            name = row["name"]
            if name in expected_names or name not in payloads:
                raise RecoveryBundleError("bundle manifest does not match archive")
            expected_names.add(name)
            payload = payloads[name]
            if row.get("bytes") != len(payload):
                raise RecoveryBundleError("bundle member size does not match manifest")
            if row.get("sha256") != hashlib.sha256(payload).hexdigest():
                raise RecoveryBundleError("bundle member hash does not match manifest")
        if expected_names != set(payloads):
            raise RecoveryBundleError("bundle contains an unlisted member")

        for name in ("data/orders.sqlite3", "data/catalog.tar.gz"):
            if name not in payloads:
                raise RecoveryBundleError("bundle is missing required business data")
        database = temporary / "orders.sqlite3"
        catalog = temporary / "catalog.tar.gz"
        database.write_bytes(payloads["data/orders.sqlite3"])
        catalog.write_bytes(payloads["data/catalog.tar.gz"])
        database.chmod(0o600)
        catalog.chmod(0o600)
        verify_sqlite_backup(database)
        catalog_result = verify_catalog_archive(catalog)

        release = manifest.get("release_commit")
        if not isinstance(release, str) or not COMMIT_RE.fullmatch(release):
            raise RecoveryBundleError("bundle release marker is invalid")
        if extract_directory is not None:
            if extract_directory.exists() or extract_directory.is_symlink():
                raise RecoveryBundleError("recovery extraction directory must not exist")
            extract_directory.mkdir(parents=True, mode=0o700)
            extract_directory.chmod(0o700)
            for name, payload in payloads.items():
                target = extract_directory.joinpath(*PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.parent.chmod(0o700)
                with target.open("xb") as output:
                    output.write(payload)
                target.chmod(0o600)
            manifest_target = extract_directory / "manifest.json"
            with manifest_target.open("xb") as output:
                output.write(
                    json.dumps(
                        manifest,
                        ensure_ascii=False,
                        sort_keys=True,
                        indent=2,
                    ).encode("utf-8")
                )
            manifest_target.chmod(0o600)
        return {
            "status": "ok",
            "release_commit": release,
            "files": len(payloads),
            **catalog_result,
        }


def _create_command(args: argparse.Namespace) -> None:
    required = tuple(args.required_path) if args.required_path else DEFAULT_REQUIRED_PATHS
    optional = tuple(args.optional_path) if args.optional_path else DEFAULT_OPTIONAL_PATHS
    output = create_bundle(
        backup_directory=args.backup_directory,
        recipient_certificate=args.recipient_certificate,
        output_directory=args.output_directory,
        required_paths=required,
        optional_paths=optional,
        destination=args.destination,
        ssh_identity=args.ssh_identity,
        known_hosts=args.known_hosts,
        max_age_hours=args.max_age_hours,
    )
    print(
        json.dumps(
            {"status": "ok", "bundle": output.name, "uploaded": bool(args.destination)},
            sort_keys=True,
        )
    )


def _verify_command(args: argparse.Namespace) -> None:
    print(
        json.dumps(
            verify_bundle(
                bundle=args.bundle,
                recipient_certificate=args.recipient_certificate,
                private_key=args.private_key,
                extract_directory=args.extract_directory,
            ),
            sort_keys=True,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--backup-directory", type=Path, default=Path("/var/backups/chainya-shop"))
    create.add_argument("--recipient-certificate", type=Path, required=True)
    create.add_argument("--output-directory", type=Path, default=Path("/var/backups/chainya-shop/offsite-out"))
    create.add_argument("--required-path", type=Path, action="append")
    create.add_argument("--optional-path", type=Path, action="append")
    create.add_argument("--destination")
    create.add_argument("--ssh-identity", type=Path)
    create.add_argument("--known-hosts", type=Path)
    create.add_argument("--max-age-hours", type=float, default=36)
    create.set_defaults(handler=_create_command)

    verify = commands.add_parser("verify")
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--recipient-certificate", type=Path, required=True)
    verify.add_argument("--private-key", type=Path, required=True)
    verify.add_argument("--extract-directory", type=Path)
    verify.set_defaults(handler=_verify_command)

    args = parser.parse_args()
    try:
        args.handler(args)
    except (
        BackupVerificationError,
        RecoveryBundleError,
        OSError,
        shutil.Error,
    ) as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    os.umask(0o077)
    main()
