"""Runtime configuration, loaded from environment variables (prefix ``PROXMOX_``)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from proxmox_ops_mcp.security.tiers import AccessTier


class Settings(BaseSettings):
    """All settings for the server.

    Authentication is API-token only. Password logins are deliberately unsupported:
    tokens can be scoped with privilege separation and revoked independently.
    """

    model_config = SettingsConfigDict(
        env_prefix="PROXMOX_",
        # Deliberately no implicit ``.env``: MCP clients start servers inside arbitrary
        # project directories, and a planted file must not be able to raise the access tier.
        # Use ``proxmox-ops-mcp --env-file PATH`` to opt in.
        env_file=None,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: Annotated[str, Field(min_length=1, description="Hostname or IP of any cluster node.")]
    port: Annotated[int, Field(ge=1, le=65535)] = 8006
    token_id: Annotated[
        str,
        Field(
            pattern=r"^[^@!\s]+@[^@!\s]+![A-Za-z][A-Za-z0-9._-]*$",
            description="API token id in the form 'user@realm!tokenname'.",
        ),
    ]
    token_secret: SecretStr
    verify_ssl: bool = True
    ca_bundle: Path | None = Field(
        default=None, description="Custom CA bundle for self-signed cluster certificates."
    )
    tls_fingerprint: str | None = Field(
        default=None,
        description="Pin the server certificate by its SHA-256 fingerprint instead of "
        "verifying CA and hostname (like proxmox-backup-client does).",
    )
    timeout: Annotated[int, Field(ge=1, le=300)] = 30

    access_tier: AccessTier = AccessTier.READ_ONLY
    audit_log: Path | None = Field(
        default=None,
        description="Append-only JSONL audit log. If unset, audit events go to stderr.",
    )
    confirmation_ttl: Annotated[int, Field(ge=30, le=3600)] = 300
    write_pools: str = Field(
        default="", description="Comma-separated pools; if set, writes only touch their guests."
    )
    write_vmids: str = Field(
        default="", description="Comma-separated VMIDs that may be written to (adds to pools)."
    )
    max_writes_per_hour: Annotated[int, Field(ge=1, le=1000)] = 30

    @field_validator("host")
    @classmethod
    def _strip_scheme(cls, value: str) -> str:
        """Accept ``https://pve.example.com:8006`` and reduce it to the bare host."""
        value = value.strip()
        hostname = urlsplit(value if "://" in value else f"//{value}").hostname
        if not hostname:
            raise ValueError(f"cannot parse a hostname from {value!r}")
        return hostname

    @field_validator("tls_fingerprint")
    @classmethod
    def _normalise_fingerprint(cls, value: str | None) -> str | None:
        """Accept ``AA:BB:...`` as printed by Proxmox and openssl; store as lowercase hex."""
        if value is None or not value.strip():
            return None
        normalised = value.strip().replace(":", "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalised):
            raise ValueError("tls_fingerprint must be a SHA-256 fingerprint (64 hex digits)")
        return normalised

    @field_validator("write_vmids")
    @classmethod
    def _check_vmids(cls, value: str) -> str:
        """Reject anything that is not a comma-separated list of VMIDs."""
        for part in _split(value):
            if not part.isdigit():
                raise ValueError(f"write_vmids must be numeric VMIDs, got {part!r}")
        return value

    @property
    def write_pool_set(self) -> frozenset[str]:
        """Parsed ``write_pools``."""
        return frozenset(_split(self.write_pools))

    @property
    def write_vmid_set(self) -> frozenset[int]:
        """Parsed ``write_vmids``."""
        return frozenset(int(v) for v in _split(self.write_vmids))

    @property
    def token_user(self) -> str:
        """The ``user@realm`` part of the token id."""
        return self.token_id.split("!", 1)[0]

    @property
    def token_name(self) -> str:
        """The token name part of the token id."""
        return self.token_id.split("!", 1)[1]

    @property
    def tls_verify(self) -> bool | str:
        """Value for ``requests``' ``verify`` parameter.

        With a pinned fingerprint, CA/hostname verification is replaced by the pin.
        """
        if not self.verify_ssl or self.tls_fingerprint:
            return False
        return str(self.ca_bundle) if self.ca_bundle else True


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]
