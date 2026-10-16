"""通过完整 Core 请求入口验证批次受理与共享请求时间。"""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
import hashlib
import json

import pytest

from open_android_intelligence_gateway.core import create_gateway_core
from test_support import make_secret_store, make_verified_request


ACCOUNT_ID = "acct_batch_boundary"
PREFIX = "/open-android-intelligence/v2"
NOW = datetime(2026, 10, 10, 0, 0, tzinfo=timezone.utc)
BATCH = {
    "clientBatchId": "cb_boundary",
    "joinMode": "newline-v1",
    "members": [
        {"clientMessageId": "cm_first", "text": "第一条"},
        {"clientMessageId": "cm_second", "text": "第二条"},
    ],
}


def _core_and_conversation(tmp_path):
    core = create_gateway_core(tmp_path, secret_store=make_secret_store())
    account = core.open_gateway_account(ACCOUNT_ID)
    try:
        conversation = account.conversations.create("cc_boundary", "原始标题", "cor_create", NOW)
    finally:
        account.close()
    return core, conversation["conversationId"]


def _request(method, target, body=None, request_id="req_boundary"):
    return make_verified_request({
        "context": {
            "accountId": ACCOUNT_ID,
            "deviceId": "dev_boundary",
            "sessionId": "sess_boundary",
            "pairingGeneration": 1,
            "grantRevision": 1,
            "requestId": request_id,
            "correlationId": request_id,
        },
        "method": method,
        "target": target,
        "body": body,
        "idempotencyKey": request_id if method != "GET" else None,
        "now": NOW,
    })


def test_batch_handle_accepts_every_member_and_preserves_idempotent_receipt(tmp_path):
    core, conversation_id = _core_and_conversation(tmp_path)
    target = f"{PREFIX}/conversations/{conversation_id}/message-batches"

    response = core.handle(_request("POST", target, BATCH))

    assert "data" in response, response
    receipt = response["data"]
    assert receipt["status"] == "accepted"
    assert [member["clientMessageId"] for member in receipt["members"]] == ["cm_first", "cm_second"]
    assert len({member["messageId"] for member in receipt["members"]}) == 2
    assert core.handle(_request("POST", target, BATCH)) == response
    account = core.open_gateway_account(ACCOUNT_ID)
    try:
        dispatch = account.conversations.claim_dispatch("cm_first", now=NOW)
        assert dispatch["text"] == "第一条\n第二条"
        assert account.conversations.claim_dispatch("cm_second", now=NOW) is None
    finally:
        account.close()


def _running_generation(tmp_path):
    core, conversation_id = _core_and_conversation(tmp_path)
    account = core.open_gateway_account(ACCOUNT_ID)
    try:
        with account.store.transaction():
            receipt = account.conversations.workflow.accept_batch(
                conversation_id, BATCH, {
                    "deviceId": "dev_boundary", "requestId": "req_seed", "correlationId": "cor_seed",
                }, NOW,
            )
        account.conversations.claim_dispatch("cm_first", now=NOW)
    finally:
        account.close()
    return core, conversation_id, receipt["generationId"]


@pytest.mark.parametrize("body", [
    {"clientMessageId": "cm_cancel_identity"},
    {"clientMessageId": "x" * 128},
    {"requestId": "req_cancel"},
], ids=["canonical", "canonical-max-id", "legacy"])
def test_cancel_handle_returns_native_receipt_and_replays_once(tmp_path, body):
    core, conversation_id, generation_id = _running_generation(tmp_path)
    calls = []

    def cancel(account_id, cid, generation_id):
        calls.append((account_id, cid, generation_id))
        return "CANCELLED"

    core.generation_canceller = cancel
    target = f"{PREFIX}/conversations/{conversation_id}/generations/{generation_id}/cancel"
    original_body = dict(body)
    request = _request("POST", target, body, "req_cancel")

    response = core.handle(request)

    assert response.get("data") == {"outcome": "CANCELLED", "generationId": generation_id}, response
    assert core.handle(request) == response
    assert calls == [(ACCOUNT_ID, conversation_id, generation_id)]
    assert body == original_body
    account = core.open_gateway_account(ACCOUNT_ID)
    try:
        # Hash the original accepted body, including its original spelling and
        # client identity. The route must never rewrite it into a legacy body.
        expected_hash = hashlib.sha256(json.dumps({"method": "POST", "target": target, "body": original_body},
            sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        saved = account.store.database.execute(
            "SELECT input_hash FROM idempotency_ledger WHERE device_id=? AND request_id=?",
            ("dev_boundary", "req_cancel"),
        ).fetchone()
        assert saved[0] == expected_hash
    finally:
        account.close()
    # A different valid canonical body or a switch between accepted spellings
    # still conflicts under the original authenticated request/idempotency ID.
    for changed in ({"clientMessageId": "cm_changed_identity"},
                    {"requestId": "req_cancel"} if "clientMessageId" in body else {"clientMessageId": "cm_cancel_identity"}):
        conflict = core.handle(_request("POST", target, changed, "req_cancel"))
        assert conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert calls == [(ACCOUNT_ID, conversation_id, generation_id)]
    current = core.handle(_request("GET", f"{PREFIX}/conversations/{conversation_id}/generations/current"))
    assert current["data"]["generation"] is None


@pytest.mark.parametrize("body", [
    {}, None, [],
    {"clientMessageId": ""}, {"clientMessageId": "bad id"}, {"clientMessageId": "x" * 129},
    {"clientMessageId": None}, {"clientMessageId": 7}, {"clientMessageId": True},
    {"clientMessageId": "cm_cancel", "requestId": "req_cancel"},
    {"clientMessageId": "cm_cancel", "extra": None},
    {"requestId": "req_other"}, {"requestId": "req_cancel", "extra": None},
])
def test_cancel_invalid_body_cannot_reach_host_or_change_running_generation(tmp_path, body):
    core, conversation_id, generation_id = _running_generation(tmp_path)
    calls = []
    core.generation_canceller = lambda *args: calls.append(args) or "CANCELLED"
    target = f"{PREFIX}/conversations/{conversation_id}/generations/{generation_id}/cancel"
    rejected = core.handle(_request("POST", target, body, "req_cancel"))
    assert rejected["error"]["code"] == "SCHEMA_INVALID"
    assert calls == []
    current = core.handle(_request("GET", f"{PREFIX}/conversations/{conversation_id}/generations/current"))
    assert current["data"]["generation"]["state"] == "running"


def test_cancel_still_requires_idempotency_key_to_match_authenticated_request(tmp_path):
    core, conversation_id, generation_id = _running_generation(tmp_path)
    calls = []
    core.generation_canceller = lambda *args: calls.append(args) or "CANCELLED"
    target = f"{PREFIX}/conversations/{conversation_id}/generations/{generation_id}/cancel"
    request = _request("POST", target, {"clientMessageId": "cm_distinct_identity"}, "req_cancel")
    request = replace(request, idempotency_key="cm_distinct_identity")
    response = core.handle(request)
    assert response["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert calls == []


def test_queued_cancel_fences_claim_and_emits_terminal_status_for_every_member(tmp_path):
    core, conversation_id = _core_and_conversation(tmp_path)
    context = {"deviceId": "dev_boundary", "pairingGeneration": 1,
        "requestId": "req_seed", "correlationId": "cor_seed"}
    account = core.open_gateway_account(ACCOUNT_ID)
    try:
        with account.store.transaction():
            receipt = account.conversations.workflow.accept_batch(conversation_id, BATCH, context, NOW)
        gid = receipt["generationId"]
        assert account.conversations.workflow.prepare_cancel(conversation_id, gid, context, None) == "CANCELLED"
        assert account.conversations.claim_dispatch("cm_first", now=NOW) is None
    finally:
        account.close()
    target = f"{PREFIX}/conversations/{conversation_id}/generations/{gid}/cancel"
    request = _request("POST", target, {"clientMessageId": "cm_cancel_queued"}, "req_cancel_queued")
    response = core.handle(request)
    assert response["data"]["outcome"] == "CANCELLED"
    assert core.handle(request) == response
    account = core.open_gateway_account(ACCOUNT_ID)
    try:
        terminal = [e["payload"] for e in account.events.read_after(None, NOW)
            if e["eventType"] == "conversation.message.status" and e["payload"]["status"] == "failed"]
        assert {p["clientMessageId"] for p in terminal} == {"cm_first", "cm_second"}
        assert len(terminal) == 2
        assert all(p["generationId"] == gid and p["errorCode"] == "AGENT_UNAVAILABLE" for p in terminal)
    finally:
        account.close()


def test_snapshot_handle_uses_request_time_without_entering_rename_branch(tmp_path):
    core, conversation_id = _core_and_conversation(tmp_path)

    response = core.handle(_request("GET", f"{PREFIX}/sync/snapshot"))

    assert "data" in response, response
    data = response["data"]
    assert [row["conversationId"] for row in data["conversations"]] == [conversation_id]
    assert data["pendingDeviceRequests"] == []
    events = core.handle(_request("GET", f"{PREFIX}/events"))["data"]["events"]
    baseline = next(event for event in events if event["eventId"] == data["baselineCursor"])
    assert baseline["eventType"] == "gateway.notice"
    assert baseline["occurredAt"] == "2026-10-10T00:00:00.000Z"
