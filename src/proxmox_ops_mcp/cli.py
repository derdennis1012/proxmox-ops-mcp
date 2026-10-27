"""Command-line entry point: ``proxmox-ops-mcp [serve|check] ...``."""

from __future__ import annotations

import argparse
import ipaddress
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError as PydanticValidationError

from proxmox_ops_mcp import __version__
from proxmox_ops_mcp.config import Settings
from proxmox_ops_mcp.errors import ProxmoxOpsError

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="proxmox-ops-mcp",
        description="MCP server for operating Proxmox VE.",
        epilog="Configuration is read from PROXMOX_* environment variables (or --env-file).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--env-file",
        type=Path,
        help="load PROXMOX_* settings from this file (environment variables take precedence)",
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the MCP server (default)")
    serve.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="stdio for local clients (default), streamable-http for remote use",
    )
    serve.add_argument("--host", default="127.0.0.1", help="bind address for HTTP transport")
    serve.add_argument("--port", type=int, default=8000, help="port for HTTP transport")
    serve.add_argument(
        "--allow-remote",
        action="store_true",
        help="permit binding the HTTP transport to a non-loopback address. The server has "
        "no authentication of its own: only do this behind an authenticating reverse proxy",
    )
    serve.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        metavar="HOST[:PORT]",
        help="additional Host header to accept on the HTTP transport (DNS-rebinding "
        "protection), e.g. the public name of your reverse proxy. Repeatable",
    )
    serve.add_argument("--log-level", default="WARNING", help="Python log level")

    sub.add_parser("check", help="verify configuration and API connectivity, then exit")
    return parser


def _load_settings(env_file: Path | None) -> Settings:
    if env_file is not None and not env_file.is_file():
        raise SystemExit(f"error: env file not found: {env_file}")
    try:
        return Settings(_env_file=env_file)  # remaining fields come from the environment
    except PydanticValidationError as exc:
        fields = ", ".join(
            "PROXMOX_" + str(e["loc"][0]).upper() for e in exc.errors() if e.get("loc")
        )
        raise SystemExit(
            f"error: invalid or missing configuration: {fields}\n"
            "See https://github.com/derdennis1012/proxmox-ops-mcp/blob/main/docs/configuration.md"
        ) from None


def _check(settings: Settings) -> int:
    from proxmox_ops_mcp.client import ProxmoxerClient  # noqa: PLC0415

    client = ProxmoxerClient.from_settings(settings)
    try:
        version = client.get("version") or {}
        nodes = client.get("nodes") or []
    except ProxmoxOpsError as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        if "SSLError" in str(exc) and not settings.tls_fingerprint:
            print(
                "HINT  Proxmox certificates often do not cover the address you connect to.\n"
                "      Set PROXMOX_CA_BUNDLE and use the node's hostname, or pin the "
                "certificate:\n      PROXMOX_TLS_FINGERPRINT=$(openssl s_client -connect "
                f"{settings.host}:{settings.port} </dev/null 2>/dev/null | openssl x509 "
                "-noout -fingerprint -sha256 | cut -d= -f2)",
                file=sys.stderr,
            )
        return 1
    print(f"OK    connected to {settings.host}:{settings.port} as {settings.token_id}")
    print(f"      Proxmox VE {version.get('version', '?')} (release {version.get('release', '?')})")
    print(f"      {len(nodes)} node(s) visible, access tier: {settings.access_tier}")
    if settings.tls_fingerprint:
        print("      TLS: certificate pinned by SHA-256 fingerprint")
    elif not settings.verify_ssl:
        print("WARN  TLS verification is disabled (PROXMOX_VERIFY_SSL=false)", file=sys.stderr)
    return 0


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _transport_security(extra_hosts: Sequence[str]) -> TransportSecuritySettings:
    """Always enforce Host/Origin checks, not only for the exact loopback strings."""
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    for host in extra_hosts:
        hosts.append(host if ":" in host.rsplit("]", 1)[-1] else f"{host}:*")
    origins = [f"http://{h}" for h in hosts] + [f"https://{h}" for h in hosts]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    args = _parser().parse_args(argv)
    command = args.command or "serve"
    transport = getattr(args, "transport", "stdio")
    if transport == "streamable-http" and not args.allow_remote and not _is_loopback(args.host):
        print(
            f"error: refusing to expose the unauthenticated HTTP transport on {args.host}. "
            "Bind to 127.0.0.1, or pass --allow-remote if an authenticating proxy is in front.",
            file=sys.stderr,
        )
        return 2
    settings = _load_settings(args.env_file)

    if command == "check":
        return _check(settings)

    # Logs must never go to stdout: with the stdio transport it carries the protocol.
    logging.basicConfig(
        level=getattr(args, "log_level", "WARNING").upper(), format=_LOG_FORMAT, stream=sys.stderr
    )
    # proxmoxer logs full request/response bodies at DEBUG/INFO, which would put guest
    # configuration (and anything secret in it) into the logs.
    logging.getLogger("proxmoxer").setLevel(logging.WARNING)
    from proxmox_ops_mcp.server import build_server  # noqa: PLC0415

    server = build_server(settings)
    if transport == "streamable-http":
        server.run(
            "streamable-http",
            host=args.host,
            port=args.port,
            transport_security=_transport_security(args.allowed_host),
        )
    else:
        server.run("stdio")
    return 0
