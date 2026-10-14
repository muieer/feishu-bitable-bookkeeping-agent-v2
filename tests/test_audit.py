import json
import logging
import stat

import pytest

from bookkeeping.audit import RequestBudget


def test_credentials_are_redacted_from_nested_data_and_exceptions(settings, audit):
    audit.event(
        "secret_check",
        body={
            "app_secret": settings.lark_app_secret.get_secret_value(),
            "nested": ["Bearer unrelated-credential-value"],
        },
        exception="failed: " + settings.ark_api_key.get_secret_value(),
        ws="wss://example.test/ws?ticket=private",
        base=settings.bitable_base_token.get_secret_value(),
    )
    raw = (settings.private_dir / "events.jsonl").read_text()
    assert all(secret not in raw for secret in settings.secrets)
    assert "unrelated-credential-value" not in raw and "ticket=private" not in raw
    assert json.loads(raw)["body"]["app_secret"] == "[REDACTED]"
    assert stat.S_IMODE((settings.private_dir / "events.jsonl").stat().st_mode) == 0o600


def test_live_budget_is_durable_and_never_allows_request_eleven(tmp_path):
    path = tmp_path / "count.jsonl"
    budget = RequestBudget(10, path)
    for expected in range(1, 11):
        assert budget.take() == expected
    with pytest.raises(RuntimeError):
        budget.take()
    restarted = RequestBudget(10, path)
    assert restarted.count == 10
    with pytest.raises(RuntimeError):
        restarted.take()


def test_sdk_errors_are_redacted_single_line_json(settings, audit):
    audit.capture_sdk_logs()
    logging.getLogger("Lark").error(
        "failed API key %s\nextra diagnostic line", settings.ark_api_key.get_secret_value()
    )
    lines = (settings.private_dir / "events.jsonl").read_text().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event"] == "sdk_log" and event["level"] == "ERROR"
    assert settings.ark_api_key.get_secret_value() not in event["message"]


def test_late_sdk_logging_cannot_reopen_closed_audit_file(settings, audit, monkeypatch):
    audit.capture_sdk_logs()
    audit.event("shutdown_complete")
    before = (settings.private_dir / "events.jsonl").read_text()
    audit.close()

    def fail_if_reopened():
        raise AssertionError("Closed audit log must not be reopened at interpreter shutdown")

    monkeypatch.setattr(audit.handler, "_open", fail_if_reopened)
    logging.getLogger("asyncio").error(
        "late task diagnostic: %s", settings.ark_api_key.get_secret_value()
    )
    assert (settings.private_dir / "events.jsonl").read_text() == before
