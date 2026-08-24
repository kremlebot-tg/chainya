from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import tarfile
import urllib.error
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
REMOTE = ROOT / "ops/deploy-shop-remote.sh"


def load_release_verifier():
    spec = importlib.util.spec_from_file_location(
        "chainya_release_verifier", ROOT / "scripts/verify-release.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_verifier_retries_one_transient_tls_failure(monkeypatch) -> None:
    verifier = load_release_verifier()
    calls = 0

    class Response:
        status = 200
        headers = type("Headers", (), {"get_content_type": lambda self: "text/html"})()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def open_once_failed(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.URLError("temporary TLS EOF")
        return Response()

    monkeypatch.setattr(verifier.urllib.request, "urlopen", open_once_failed)
    monkeypatch.setattr(verifier.time, "sleep", lambda _seconds: None)

    assert verifier.response_metadata("https://chainya.invalid/")[:2] == (200, "text/html")
    assert calls == 2


def write_tar(path: Path, files: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))


def make_candidate(tmp_path: Path, *, nginx_changed: bool = False) -> tuple[Path, Path]:
    root = tmp_path / "root"
    stage = tmp_path / "stage"
    for path in (
        root / "opt/chainya-shop-releases/old-backend",
        root / "var/www/chainya-releases/old-web",
        root / "var/lib/chainya-shop",
        root / "var/backups/chainya-shop",
        root / "etc/systemd/system",
        root / "etc/nginx/sites-available",
        root / "run",
        stage,
    ):
        path.mkdir(parents=True, exist_ok=True)
    (root / "opt/chainya-shop").symlink_to(
        root / "opt/chainya-shop-releases/old-backend"
    )
    (root / "var/www/chainya").symlink_to(root / "var/www/chainya-releases/old-web")
    (root / "var/lib/chainya-shop/orders.sqlite3").write_bytes(b"old-db")
    (root / "var/lib/chainya-shop/catalog.json").write_text(
        '{"revision":1,"teas":[]}', encoding="utf-8"
    )
    (root / "var/lib/chainya-shop").chmod(0o750)
    (root / "var/lib/chainya-shop/web-release-commit").write_text(
        "0" * 40 + "\n", encoding="ascii"
    )
    for state in (
        "chainya-shop.active",
        "chainya-backup.timer.active",
        "chainya-backup.timer.enabled",
    ):
        (root / "run" / state).touch()

    units = {
        "chainya-shop.service": "old shop unit\n",
        "chainya-backup.service": "old backup unit\n",
        "chainya-backup.timer": "old backup timer\n",
    }
    for name, content in units.items():
        (root / "etc/systemd/system" / name).write_text(content, encoding="utf-8")
        (stage / name).write_text(content, encoding="utf-8")
    active_nginx = "server { server_name chainya.ru; }\n"
    candidate_nginx = active_nginx + ("# candidate change\n" if nginx_changed else "")
    (root / "etc/nginx/sites-available/chainya.ru").write_text(
        active_nginx, encoding="utf-8"
    )
    (stage / "nginx-chainya.ru").write_text(candidate_nginx, encoding="utf-8")
    (stage / "RELEASE_COMMIT").write_text("a" * 40 + "\n", encoding="ascii")
    write_tar(stage / "shop.tgz", {"backend/app.py": b"pass\n"})
    write_tar(stage / "web.tgz", {"index.html": b"new web\n"})
    return root, stage


def invoke(root: Path, stage: Path, action: str, *, failpoint: str = "") -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "CHAINYA_DEPLOY_TEST_ROOT": str(root),
            "CHAINYA_STAGE": str(stage),
            "CHAINYA_DEPLOY_FAILPOINT": failpoint,
        }
    )
    return subprocess.run(
        ["bash", str(REMOTE), action],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def assert_old_release_restored(root: Path) -> None:
    assert (root / "opt/chainya-shop").resolve().name == "old-backend"
    assert (root / "var/www/chainya").resolve().name == "old-web"
    assert (root / "var/lib/chainya-shop/orders.sqlite3").read_bytes() == b"old-db"
    assert (root / "var/lib/chainya-shop/web-release-commit").read_text().strip() == "0" * 40
    assert (root / "var/lib/chainya-shop").stat().st_mode & 0o777 == 0o750
    assert (root / "run/chainya-shop.active").exists()
    assert (root / "run/chainya-backup.timer.active").exists()


def test_static_deploy_contract() -> None:
    result = subprocess.run(
        ["python3", str(ROOT / "scripts/check-deploy-contract.py")],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    deploy = (ROOT / "deploy-shop.sh").read_text(encoding="utf-8")
    assert "remote_uploaded=1" in deploy
    assert 'rm -rf -- \'$REMOTE_STAGE\'' in deploy
    first_archive = deploy.index("COPYFILE_DISABLE=1 tar")
    second_archive = deploy.index("COPYFILE_DISABLE=1 tar", first_archive + 1)
    archive_command = deploy[first_archive:second_archive]
    assert "backend ops scripts telegram-bot" in archive_command
    assert "deploy.sh deploy-shop.sh" in archive_command
    assert "deploy-edge.sh" in archive_command
    assert "deploy-shop.sh" in archive_command
    assert "deploy-bot.sh" in archive_command
    assert "telegram-bot" in archive_command
    assert 'BOT_ROOT="$ROOT/telegram-bot"' in deploy
    assert 'BOT_ROOT="$ROOT/../telegram-bot"' not in deploy
    remote = REMOTE.read_text(encoding="utf-8")
    assert "for _attempt in {1..30}" in remote
    assert "payload=$(curl" in remote
    assert "return 1" in remote


def test_legacy_static_deploy_never_controls_shared_nginx() -> None:
    deploy = (ROOT / "deploy.sh").read_text(encoding="utf-8")
    for command in (
        "systemctl stop nginx",
        "systemctl start nginx",
        "systemctl restart nginx",
        "service nginx stop",
        "service nginx start",
        "service nginx restart",
    ):
        assert command not in deploy
    assert "legacy-каталогом" in deploy
    assert "systemctl is-active --quiet nginx" in deploy


def test_bot_deploy_uses_versioned_repository_sources() -> None:
    deploy = (ROOT / "deploy-bot.sh").read_text(encoding="utf-8")
    assert 'BOT_ROOT="$ROOT/telegram-bot"' in deploy
    assert "../telegram-bot" not in deploy
    assert 'git -C "$ROOT" ls-files --error-unmatch "telegram-bot/$file"' in deploy
    for required in (
        "bot.py",
        "requirements.txt",
        "requirements.lock.txt",
        "teas.json",
        "test_bot_booking.py",
        "media/start.jpg",
    ):
        assert (ROOT / "telegram-bot" / required).is_file(), required
    assert 'requirements.lock.txt"' in deploy
    assert 'pip" install -q -r "$release/requirements.lock.txt"' in deploy
    catalog_builder = (ROOT / "scripts/build-catalog-seed.py").read_text(encoding="utf-8")
    assert 'BOT_CATALOG = ROOT / "telegram-bot" / "teas.json"' in catalog_builder
    assert 'ROOT.parent / "telegram-bot"' not in catalog_builder


def test_maintenance_is_chainya_only_and_returns_503() -> None:
    internal = (ROOT / "ops/timeweb/Caddyfile.internal").read_text(encoding="utf-8")
    public = (ROOT / "ops/timeweb/Caddyfile.public-snippet").read_text(encoding="utf-8")
    compose = (ROOT / "ops/timeweb/docker-compose.edge.yml").read_text(encoding="utf-8")
    assert "http://127.0.0.1:8078" in internal
    assert "chainya-maintenance.enabled" in internal
    assert " 503" in internal
    assert "chainya.ru" in public and "127.0.0.1:8078" in public
    assert "__chainya_edge_health" in internal
    assert "__chainya_edge_health" in compose
    assert "/api/health" not in compose
    assert "/manage/*" in internal
    assert "@productPage path_regexp" in internal
    assert "@sitemap path /sitemap.xml" in internal
    assert "redir @productSlash /{re.productSlash.1} 308" in internal
    assert "(en/|zh/)?(tea|teaware)/" in internal
    assert "redir /teaware/" not in internal
    assert "@publicApp path / /shop /teaware /teaware/ /business /booking" in internal
    assert "@localizedHome path /en/ /zh/" in internal
    assert "@localizedApp path_regexp localizedApp" in internal
    assert "(?:en|zh)/(?:shop|teaware|business|booking)" in internal
    nginx = (ROOT / "ops/nginx-chainya.ru").read_text(encoding="utf-8")
    assert "location = /manage/guides" in nginx
    assert "location = /manage/site" in nginx
    assert "(?:catalog|site)\\.js" in nginx
    assert 'location ~ "^/(?:en/|zh/)?(?:tea|teaware)/' in nginx
    assert "location ~ ^/(en|zh)/(shop|teaware|business|booking)$" in nginx
    assert "try_files /$1/$2/index.html =404;" in nginx
    assert "return 308 /$1;" in nginx
    assert "location = /sitemap.xml" in nginx


def test_edge_config_change_is_validated_and_rolls_back_only_chainya_edge() -> None:
    deploy = (ROOT / "deploy-edge.sh").read_text(encoding="utf-8")
    assert 'cp ops/timeweb/Caddyfile.internal "$TMP/Caddyfile.internal"' in deploy
    assert "caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile" in deploy
    assert 'test "$maintenance" = 1' in deploy
    assert 'cp -p "$edge_config" "$stage/Caddyfile.previous"' in deploy
    assert 'mv -Tf "${edge_config}.rollback" "$edge_config"' in deploy
    assert "docker compose -f \"$edge_compose\" up -d --no-deps --force-recreate edge" in deploy
    assert "systemctl" not in deploy


def test_origin_upload_retries_before_any_cutover() -> None:
    deploy = (ROOT / "deploy-shop.sh").read_text(encoding="utf-8")
    retry = deploy.index("for upload_attempt in 1 2 3")
    stage = deploy.index("remote_transaction stage")
    maintenance = deploy.index('echo "→ Chainya-only maintenance"')
    cutover = deploy.index("remote_transaction cutover")
    assert "rsync -az --partial" in deploy
    assert retry < stage < maintenance < cutover


def test_remote_transaction_survives_transient_ssh_disconnect() -> None:
    deploy = (ROOT / "deploy-shop.sh").read_text(encoding="utf-8")
    assert "ServerAliveInterval=15" in deploy
    assert "ServerAliveCountMax=12" in deploy
    assert "nohup bash -c" in deploy
    assert 'lock="${stage}.operation-${operation}"' in deploy
    assert 'printf "%s\\n" "$result" >"$lock/status.next"' in deploy
    assert 'mv -f -- "$lock/status.next" "$lock/status"' in deploy
    assert ".operation-${operation}/output.log" in deploy


def test_successful_cutover_and_commit(tmp_path: Path) -> None:
    root, stage = make_candidate(tmp_path)
    assert invoke(root, stage, "stage").returncode == 0
    result = invoke(root, stage, "cutover")
    assert result.returncode == 0, result.stderr
    assert (root / "opt/chainya-shop").resolve().name.startswith("a" * 12)
    assert (root / "var/www/chainya").resolve().name.startswith("a" * 12)
    assert (root / "run/chainya-shop.active").exists()
    assert (stage / "transaction/phase").read_text().strip() == "prepared"
    assert invoke(root, stage, "commit").returncode == 0
    assert not stage.exists()
    actions = (root / "run/service-actions.log").read_text()
    assert "stop nginx" not in actions
    assert "start nginx" not in actions
    assert "restart nginx" not in actions
    assert "reload nginx" not in actions
    assert "test nginx" not in actions


def test_stage_failpoint_removes_partial_transaction(tmp_path: Path) -> None:
    root, stage = make_candidate(tmp_path)
    result = invoke(root, stage, "stage", failpoint="after_stage")
    assert result.returncode != 0
    assert_old_release_restored(root)
    assert not (root / "run/chainya-deploy.transaction").exists()
    assert not (stage / "transaction").exists()


@pytest.mark.parametrize(
    "failpoint",
    [
        "after_stop",
        "after_state_snapshot",
        "after_config",
        "after_symlink",
        "after_start",
        "after_health",
    ],
)
def test_failpoints_restore_old_release(tmp_path: Path, failpoint: str) -> None:
    root, stage = make_candidate(tmp_path)
    assert invoke(root, stage, "stage").returncode == 0
    result = invoke(root, stage, "cutover", failpoint=failpoint)
    assert result.returncode != 0
    assert_old_release_restored(root)
    assert (stage / "transaction/phase").read_text().strip() == "rolled_back"


def test_nginx_change_uses_reload_and_rollback_reload_only(tmp_path: Path) -> None:
    root, stage = make_candidate(tmp_path, nginx_changed=True)
    original = (root / "etc/nginx/sites-available/chainya.ru").read_text()
    assert invoke(root, stage, "stage").returncode == 0
    assert invoke(root, stage, "cutover").returncode == 0
    changed = (root / "etc/nginx/sites-available/chainya.ru").read_text()
    assert changed != original
    assert invoke(root, stage, "rollback").returncode == 0
    assert (root / "etc/nginx/sites-available/chainya.ru").read_text() == original
    actions = (root / "run/service-actions.log").read_text().splitlines()
    assert actions.count("reload nginx") == 2
    assert actions.count("test nginx") == 4
    assert all(action not in {"stop nginx", "start nginx", "restart nginx"} for action in actions)


def test_failure_after_nginx_reload_restores_config_and_reloads(tmp_path: Path) -> None:
    root, stage = make_candidate(tmp_path, nginx_changed=True)
    original = (root / "etc/nginx/sites-available/chainya.ru").read_text()
    assert invoke(root, stage, "stage").returncode == 0
    result = invoke(root, stage, "cutover", failpoint="after_config")
    assert result.returncode != 0
    assert_old_release_restored(root)
    assert (root / "etc/nginx/sites-available/chainya.ru").read_text() == original
    actions = (root / "run/service-actions.log").read_text().splitlines()
    assert actions.count("reload nginx") == 2


def test_invalid_nginx_candidate_rolls_back_before_reload(tmp_path: Path) -> None:
    root, stage = make_candidate(tmp_path, nginx_changed=True)
    original = (root / "etc/nginx/sites-available/chainya.ru").read_text()
    with (stage / "nginx-chainya.ru").open("a", encoding="utf-8") as target:
        target.write("INVALID_NGINX_TEST\n")
    assert invoke(root, stage, "stage").returncode == 0
    result = invoke(root, stage, "cutover")
    assert result.returncode != 0
    assert_old_release_restored(root)
    assert (root / "etc/nginx/sites-available/chainya.ru").read_text() == original
    actions = (root / "run/service-actions.log").read_text().splitlines()
    assert actions.count("reload nginx") == 1
    assert actions.count("test nginx") == 4
