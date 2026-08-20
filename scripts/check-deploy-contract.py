#!/usr/bin/env python3
"""Fail-closed contract for the two-host Chainya release."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_FILES = (
    ROOT / "deploy.sh",
    ROOT / "deploy-shop.sh",
    ROOT / "ops" / "deploy-shop-remote.sh",
)

# Match executable command lines, not comments or strings used by this checker.
FORBIDDEN_NGINX_CONTROL = re.compile(
    r"^\s*(?:sudo\s+)?(?:"
    r"systemctl\s+(?:stop|start|restart)\s+nginx(?:\.service)?"
    r"|service\s+nginx\s+(?:stop|start|restart)"
    r")\s*(?:[#;]|$)",
    re.MULTILINE,
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def main() -> None:
    sources = {path: path.read_text(encoding="utf-8") for path in DEPLOY_FILES}
    for path, source in sources.items():
        require(
            FORBIDDEN_NGINX_CONTROL.search(source) is None,
            f"forbidden full Nginx control in {path.relative_to(ROOT)}",
        )

    deploy = sources[ROOT / "deploy-shop.sh"]
    remote = sources[ROOT / "ops" / "deploy-shop-remote.sh"]
    edge_deploy = (ROOT / "deploy-edge.sh").read_text(encoding="utf-8")
    caddy = (ROOT / "ops/timeweb/Caddyfile.internal").read_text(encoding="utf-8")
    compose = (ROOT / "ops/timeweb/docker-compose.edge.yml").read_text(
        encoding="utf-8"
    )

    require('"$MAINTENANCE" on' in deploy, "maintenance enable is missing")
    require('"$MAINTENANCE" off' in deploy, "maintenance disable is missing")
    maintenance_on = deploy.index('echo "→ Chainya-only maintenance"')
    cutover = deploy.index("remote_transaction cutover")
    maintenance_off = deploy.index('echo "→ снятие Chainya-only maintenance"')
    require(maintenance_on < cutover, "maintenance must precede origin cutover")
    require(cutover < maintenance_off, "maintenance must remain enabled through origin cutover")
    require(
        "for upload_attempt in 1 2 3" in deploy and "rsync -az --partial" in deploy,
        "origin staging upload must retry transient SSH failures",
    )
    require(
        "nohup bash -c" in deploy
        and ".operation-${operation}/status" in deploy
        and "ServerAliveInterval=15" in deploy,
        "remote operations must survive transient controlling SSH failures",
    )
    require("systemctl reload nginx" in remote, "graceful Nginx reload is missing")
    require("nginx_test" in remote, "nginx -t wrapper is missing")
    require("if [ \"$nginx_changed\" = 1 ]" in remote, "Nginx diff gate is missing")
    require("transaction/state" in remote, "consistent state snapshot is missing")
    require("rollback_core" in remote, "automatic rollback is missing")
    require("previous=$(readlink -f \"$active\")" in edge_deploy, "edge rollback target is missing")
    require("trap rollback ERR" in edge_deploy, "edge automatic rollback is missing")
    require(
        "caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile" in edge_deploy,
        "changed Chainya edge config must be validated",
    )
    require(
        'mv -Tf "${edge_config}.rollback" "$edge_config"' in edge_deploy,
        "Chainya edge config rollback is missing",
    )
    require(
        'test "$maintenance" = 1' in edge_deploy,
        "Chainya edge config changes must require maintenance",
    )
    require(
        'CHAINYA_EDGE_MAINTENANCE' in edge_deploy and ')" = 503' in edge_deploy,
        "edge maintenance verification is missing",
    )

    require("chainya-maintenance.enabled" in caddy, "maintenance marker is missing")
    require('respond "Чайня обновляется.' in caddy, "maintenance response is missing")
    require(" 503" in caddy, "maintenance must return HTTP 503")
    require("/__chainya_edge_health" in caddy, "local edge health is missing")
    require("/manage/*" in caddy, "private owner subroutes must reach the origin")
    require("@productPage path_regexp" in caddy, "product pages must reach the origin")
    require("(tea|teaware)" in caddy, "tea and teaware product pages must reach the origin")
    require("redir /teaware/" not in caddy, "teaware slash redirect can loop from browser cache")
    require(
        "@publicApp path / /shop /teaware /teaware/ /business /booking" in caddy,
        "both teaware base routes must be served as the public app",
    )
    require("@sitemap path /sitemap.xml" in caddy, "dynamic sitemap must reach the origin")
    require(
        "/__chainya_edge_health" in compose and "/api/health" not in compose,
        "container healthcheck must not call the production origin",
    )

    print("deploy contract: ok")


if __name__ == "__main__":
    main()
