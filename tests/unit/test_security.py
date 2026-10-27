from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from proxmox_ops_mcp.errors import ConfirmationError, PermissionDeniedError
from proxmox_ops_mcp.security import AccessTier, AuditLogger, ConfirmationManager, WritePolicy
from proxmox_ops_mcp.security.audit import redact


class TestAccessTier:
    def test_ordering(self) -> None:
        assert AccessTier.ADMIN.allows(AccessTier.OPERATOR)
        assert AccessTier.OPERATOR.allows(AccessTier.OPERATOR)
        assert not AccessTier.READ_ONLY.allows(AccessTier.OPERATOR)
        assert not AccessTier.OPERATOR.allows(AccessTier.ADMIN)

    def test_require_raises_with_context(self) -> None:
        with pytest.raises(PermissionDeniedError, match="requires access tier 'admin'"):
            AccessTier.OPERATOR.require(AccessTier.ADMIN, "migrate_guest")
        AccessTier.ADMIN.require(AccessTier.ADMIN, "migrate_guest")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TestConfirmationManager:
    def test_round_trip(self) -> None:
        cm = ConfirmationManager()
        token = cm.issue("guest_power:stop", {"vmid": 100, "node": "pve1"})
        cm.redeem(token, "guest_power:stop", {"node": "pve1", "vmid": 100})

    def test_token_is_single_use(self) -> None:
        cm = ConfirmationManager()
        token = cm.issue("a", {"x": 1})
        cm.redeem(token, "a", {"x": 1})
        with pytest.raises(ConfirmationError, match="already used"):
            cm.redeem(token, "a", {"x": 1})

    def test_changed_parameters_are_rejected_and_burn_the_token(self) -> None:
        cm = ConfirmationManager()
        token = cm.issue("a", {"vmid": 100})
        with pytest.raises(ConfirmationError, match="does not match"):
            cm.redeem(token, "a", {"vmid": 101})
        with pytest.raises(ConfirmationError):
            cm.redeem(token, "a", {"vmid": 100})

    def test_changed_action_is_rejected(self) -> None:
        cm = ConfirmationManager()
        token = cm.issue("guest_power:shutdown", {"vmid": 100})
        with pytest.raises(ConfirmationError):
            cm.redeem(token, "guest_power:stop", {"vmid": 100})

    def test_expiry(self) -> None:
        clock = FakeClock()
        cm = ConfirmationManager(60, clock=clock)
        token = cm.issue("a", {})
        clock.now += 61
        with pytest.raises(ConfirmationError, match="expired"):
            cm.redeem(token, "a", {})

    def test_expired_tokens_are_purged(self) -> None:
        clock = FakeClock()
        cm = ConfirmationManager(60, clock=clock)
        old = cm.issue("a", {})
        clock.now += 61
        cm.issue("b", {})
        with pytest.raises(ConfirmationError, match="Unknown"):
            cm.redeem(old, "a", {})

    def test_forged_token_is_rejected(self) -> None:
        cm = ConfirmationManager()
        with pytest.raises(ConfirmationError):
            cm.redeem("abc.def", "a", {})

    def test_tokens_do_not_survive_a_new_manager(self) -> None:
        token = ConfirmationManager().issue("a", {})
        with pytest.raises(ConfirmationError):
            ConfirmationManager().redeem(token, "a", {})


class TestAudit:
    def test_redaction_is_recursive_and_targeted(self) -> None:
        data = {
            "token_secret": "x",
            "nested": [{"password": "y", "keyboard": "de"}],
            "cipassword": "z",
            "confirmation_token": "t",
            "vmid": 100,
        }
        assert redact(data) == {
            "token_secret": "***",
            "nested": [{"password": "***", "keyboard": "de"}],
            "cipassword": "***",
            "confirmation_token": "***",
            "vmid": 100,
        }

    def test_writes_jsonl_to_stream(self) -> None:
        stream = io.StringIO()
        AuditLogger(stream=stream).record(
            action="a", outcome="previewed", tier="operator", params={"vmid": 1}
        )
        event = json.loads(stream.getvalue())
        assert event["action"] == "a"
        assert event["outcome"] == "previewed"
        assert event["params"] == {"vmid": 1}
        assert event["ts"].endswith("+00:00")

    def test_appends_to_file(self, tmp_path: Path) -> None:
        path = tmp_path / "logs" / "audit.jsonl"
        logger = AuditLogger(path)
        for outcome in ("previewed", "executed"):
            logger.record(action="a", outcome=outcome, tier="admin", params={}, detail={"k": 1})
        lines = path.read_text().splitlines()
        assert [json.loads(line)["outcome"] for line in lines] == ["previewed", "executed"]

    def test_defaults_to_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        AuditLogger().record(action="a", outcome="failed", tier="admin", params={})
        captured = capsys.readouterr()
        assert captured.out == ""
        assert '"outcome": "failed"' in captured.err


class TestWritePolicy:
    def test_unscoped_allows_everything(self) -> None:
        WritePolicy().check_scope({"vmid": 100})

    def test_scope_by_pool_and_vmid(self) -> None:
        policy = WritePolicy(pools=frozenset({"lab"}), vmids=frozenset({200}))
        policy.check_scope({"vmid": 101, "pool": "lab"})
        policy.check_scope({"vmid": 200})
        with pytest.raises(
            PermissionDeniedError, match=r"write scope of this server \(allowed: lab, 200\)"
        ):
            policy.check_scope({"vmid": 100, "pool": "prod"})

    def test_rate_limit_slides(self) -> None:
        clock = FakeClock()
        policy = WritePolicy(max_per_hour=2, clock=clock)
        policy.acquire()
        policy.acquire()
        with pytest.raises(PermissionDeniedError, match="2 per hour"):
            policy.acquire()
        clock.now += 3600
        policy.acquire()
