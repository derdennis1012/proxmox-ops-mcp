from __future__ import annotations

import io
import json
from collections.abc import Callable
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from pydantic import SecretStr

from proxmox_ops_mcp.config import Settings
from proxmox_ops_mcp.security import AccessTier, AuditLogger
from proxmox_ops_mcp.server import build_server
from tests.fakes import NOW, FakeProxmoxClient


def make_settings(tier: AccessTier = AccessTier.READ_ONLY, **overrides: Any) -> Settings:
    return Settings(
        host="pve.example.com",
        token_id="ops@pve!mcp",
        token_secret=SecretStr("00000000-0000-0000-0000-000000000000"),
        access_tier=tier,
        _env_file=None,
        **overrides,
    )


class ServerHarness:
    """A server wired to a fake API, with helpers to call tools and read the audit log."""

    def __init__(self, tier: AccessTier, client: FakeProxmoxClient, **settings: Any) -> None:
        self.client = client
        self.audit_stream = io.StringIO()
        self.server: MCPServer = build_server(
            make_settings(tier, **settings),
            client=client,
            audit=AuditLogger(stream=self.audit_stream),
            clock=lambda: float(NOW),
        )

    async def tool_names(self) -> set[str]:
        return {t.name for t in await self.server.list_tools()}

    async def call(self, tool: str, /, **arguments: Any) -> dict[str, Any]:
        result = await self.server.call_tool(tool, arguments)
        assert isinstance(result, CallToolResult)
        assert not result.is_error, result.content
        assert result.structured_content is not None
        return dict(result.structured_content)

    def audit_events(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.audit_stream.getvalue().splitlines()]


@pytest.fixture
def fake_client() -> FakeProxmoxClient:
    return FakeProxmoxClient()


@pytest.fixture
def harness(fake_client: FakeProxmoxClient) -> Callable[[AccessTier], ServerHarness]:
    def factory(tier: AccessTier = AccessTier.READ_ONLY) -> ServerHarness:
        return ServerHarness(tier, fake_client)

    return factory


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
