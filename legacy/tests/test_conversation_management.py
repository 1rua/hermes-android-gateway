"""Shared organization and actual SessionDB archive/delete, without model calls."""
from __future__ import annotations

from types import SimpleNamespace
import threading
import asyncio

import pytest

from open_android_intelligence_gateway.core import GatewayError, create_gateway_core
from open_android_intelligence_gateway.host_history import HermesHistoryPort
from test_support import make_secret_store, trust_core
from test_native_turn_lifecycle import native_store  # noqa: F401 - real SDK fixture

PREFIX = "/open-android-intelligence/v2"


@pytest.fixture
def managed(tmp_path):
    SessionDB = pytest.importorskip("hermes_state").SessionDB
    database = SessionDB(tmp_path / "native.db")
    core = trust_core(create_gateway_core(tmp_path / "gateway", secret_store=make_secret_store()))
    store = SimpleNamespace(_db=database, _lock=threading.Lock(), _entries={})
    port = HermesHistoryPort(core, store)
    core.history_reader = port.read
    core.conversation_manager = port.manage
    yield core, database, store
    database.close()


def send(core, method, path, body=None, request_id="req_management", device="dev_one", account="acct_one"):
    return core.handle({"method": method, "target": PREFIX + path, "body": body,
        "context": {"accountId": account, "deviceId": device, "sessionId": "sess_one",
            "requestId": request_id, "correlationId": "cor_management", "pairingGeneration": 1, "grantRevision": 1},
        "idempotencyKey": request_id})


def seed(core, database, count=1, account_id="acct_one"):
    account = core.open_gateway_account(account_id)
    ids = []
    try:
        for index in range(count):
            cid = account.conversations.create(f"cc_{index}", f"Thread {index}", "cor_seed")["conversationId"]
            sid = account_id + f"_native_{index}"
            database.create_session(sid, "open_android", user_id=account_id, chat_id=cid)
            database.append_message(sid, "assistant", f"Native reply {index}", timestamp=1000 + index)
            account.agent_sessions.record(cid, "inbound-message")
            account.agent_sessions.attach(cid, sid)
            ids.append((cid, sid))
        return ids
    finally:
        account.close()


def test_full_history_pages_include_archived_native_sessions_and_expire_on_change(managed):
    core, database, _ = managed
    ids = seed(core, database, 121)
    database.set_session_archived(ids[0][1], True)
    cursor = None
    rows, revisions = [], set()
    while True:
        from urllib.parse import urlencode
        page = send(core, "GET", "/conversations?" + urlencode({"limit": 17, **({"cursor": cursor} if cursor else {})}))["data"]
        rows.extend(page["conversations"])
        revisions.add(page["snapshotRevision"])
        cursor = page["nextCursor"]
        if cursor is None:
            break
    assert len(rows) == len({row["conversationId"] for row in rows}) == 121
    assert len(revisions) == 1
    assert next(row for row in rows if row["conversationId"] == ids[0][0])["archived"] is True
    first = send(core, "GET", "/conversations?limit=5")["data"]
    database.set_session_archived(ids[-1][1], True)
    expired = send(core, "GET", "/conversations?" + urlencode({"cursor": first["nextCursor"]}))
    assert expired["error"]["code"] == "CURSOR_EXPIRED"
    assert send(core, "GET", "/conversations?limit=0")["error"]["code"] == "SCHEMA_INVALID"


def test_two_devices_share_folders_order_and_optimistic_revision_without_affecting_list_snapshot(managed):
    core, database, _ = managed
    ids = seed(core, database, 2)
    before = send(core, "GET", "/conversations?limit=1")["data"]
    body = {"expectedRevision": 0, "sortMode": "custom", "customOrder": [value[0] for value in ids],
        "folders": [{"folderId": "folder_work", "name": "Work"}], "assignments": {ids[0][0]: "folder_work"}}
    result = send(core, "PUT", "/conversation-organization", body)["data"]
    assert result["revision"] == 1
    assert send(core, "GET", "/conversation-organization", device="dev_two")["data"] == result
    conflict = send(core, "PUT", "/conversation-organization", body, "req_stale", "dev_two")
    assert conflict["error"]["code"] == "ORGANIZATION_CONFLICT"
    assert send(core, "GET", "/conversations?limit=1")["data"]["snapshotRevision"] == before["snapshotRevision"]
    assert send(core, "GET", "/conversation-organization", account="acct_other")["data"]["folders"] == []
    invalid = {**body, "expectedRevision": 1, "assignments": {"conv_other_account": "folder_work"}}
    assert send(core, "PUT", "/conversation-organization", invalid, "req_foreign")["error"]["code"] == "SCHEMA_INVALID"
    removed = send(core, "PUT", "/conversation-organization", {**body, "expectedRevision": 1,
        "folders": [], "assignments": {}, "sortMode": "updated-desc"}, "req_folder_remove")["data"]
    assert removed["folders"] == [] and removed["revision"] == 2


def test_archive_delete_mutate_actual_native_compression_lineage_and_preserve_unrelated_rows(managed):
    core, database, _ = managed
    cid, sid = seed(core, database)[0]
    database.end_session(sid, "compression")
    database.create_session("compressed_tip", "open_android", parent_session_id=sid)
    database.append_message("compressed_tip", "assistant", "Continuation", timestamp=2000)
    database.create_session("unrelated", "cli")
    database.append_message("unrelated", "user", "Keep me", timestamp=3000)
    result = send(core, "PATCH", f"/conversations/{cid}", {"archived": True})
    assert result["data"]["conversation"]["archived"] is True
    assert all(database.get_session(value)["archived"] == 1 for value in [sid, "compressed_tip"])
    assert send(core, "PATCH", f"/conversations/{cid}", {"archived": False}, "req_restore")["data"]["conversation"]["archived"] is False
    send(core, "PUT", "/conversation-organization", {"expectedRevision": 0, "sortMode": "custom", "customOrder": [cid],
        "folders": [{"folderId": "f_one", "name": "One"}], "assignments": {cid: "f_one"}}, "req_organize")
    deleted = send(core, "DELETE", f"/conversations/{cid}", request_id="req_delete")
    assert deleted["data"] == {"conversationId": cid, "deleted": True}
    assert database.get_session(sid) is None and database.get_session("compressed_tip") is None
    assert database.get_messages(sid, include_inactive=True) == []
    assert database.get_messages("compressed_tip", include_inactive=True) == []
    assert database.get_messages("unrelated")[0]["content"] == "Keep me"
    assert send(core, "DELETE", f"/conversations/{cid}", request_id="req_delete") == deleted
    assert send(core, "GET", "/conversations")["data"]["conversations"] == []
    assert send(core, "GET", f"/conversations/{cid}/messages")["error"]["code"] == "CONVERSATION_NOT_FOUND"
    assert send(core, "GET", "/conversation-organization")["data"]["customOrder"] == []
    account = core.open_gateway_account("acct_one")
    try:
        with pytest.raises(GatewayError, match="CONVERSATION_NOT_FOUND"):
            account.conversations.create("cc_0", "Thread 0", "cor_retry_create")
        assert account.conversations.management.state(cid)["deleted"] is True
    finally:
        account.close()


def test_busy_queue_and_native_active_marker_refuse_delete_before_native_content_changes(managed):
    core, database, store = managed
    cid, sid = seed(core, database)[0]
    account = core.open_gateway_account("acct_one")
    try:
        receipt = account.conversations.accept_message(cid, "cm_busy", "Queued", [], "dev_one", "req_message", "cor_message")
    finally:
        account.close()
    assert send(core, "DELETE", f"/conversations/{cid}")["error"]["code"] == "CONVERSATION_BUSY"
    assert database.get_session(sid) is not None
    account = core.open_gateway_account("acct_one")
    try:
        account.conversations.workflow.settle(receipt["messageId"], "failed")
        account.store.database.execute("UPDATE conversation_message_dispatch SET status='failed' WHERE message_id=?", (receipt["messageId"],))
        account.store.database.execute("UPDATE conversation_agent_sessions SET session_key='trusted' WHERE conversation_id=?", (cid,))
    finally:
        account.close()
    store._entries["trusted"] = SimpleNamespace(session_id=sid, active_turn_token="native_work")
    assert send(core, "DELETE", f"/conversations/{cid}", request_id="req_native_busy")["error"]["code"] == "CONVERSATION_BUSY"
    assert database.get_session(sid) is not None


def test_native_delete_in_another_ui_produces_tombstone_and_external_archive_is_visible(managed):
    core, database, _ = managed
    cid, sid = seed(core, database)[0]
    database.set_session_archived(sid, True)
    assert send(core, "GET", "/conversations")["data"]["conversations"][0]["archived"] is True
    database.delete_session(sid)
    assert send(core, "GET", "/conversations")["data"]["conversations"] == []
    # Even a stale writer restoring the native row cannot undo the durable deletion.
    database.create_session(sid, "open_android")
    assert send(core, "GET", "/conversations")["data"]["conversations"] == []


def test_first_upgraded_list_tombstones_deleted_attached_session_and_keeps_unmaterialized_bindings(managed):
    core, database, _ = managed
    cid, sid = seed(core, database)[0]
    account = core.open_gateway_account("acct_one")
    try:
        untouched = account.conversations.create("cc_untouched", "Untouched", "cor_legacy")["conversationId"]
        account.agent_sessions.record(untouched, "conversation-read")
        failed = account.conversations.create("cc_failed", "Failed", "cor_legacy")["conversationId"]
        account.agent_sessions.record(failed, "inbound-message")
        receipt = account.conversations.accept_message(failed, "cm_unadopted", "Never adopted", [], "dev_one", "req_unadopted", "cor_legacy")
        account.store.database.execute("UPDATE conversation_message_dispatch SET status='failed' WHERE message_id=?", (receipt["messageId"],))
        assert account.agent_sessions.lookup(cid)["agentSessionId"] == sid
        assert account.agent_sessions.lookup(untouched)["agentSessionId"] is None
        assert account.agent_sessions.lookup(failed)["agentSessionId"] is None
        assert account.store.database.execute("SELECT 1 FROM account_metadata WHERE key=?", ("conversation-management:" + cid,)).fetchone() is None
        account.conversations.management.replace({"expectedRevision": 0, "sortMode": "custom", "customOrder": [cid],
            "folders": [{"folderId": "legacy", "name": "Legacy"}], "assignments": {cid: "legacy"}}, "cor_legacy")
    finally:
        account.close()
    # The native row disappears before any new lifecycle projection has run.
    database.delete_session(sid)
    rows = send(core, "GET", "/conversations")["data"]["conversations"]
    assert {row["conversationId"] for row in rows} == {untouched, failed}
    account = core.open_gateway_account("acct_one")
    try:
        assert account.conversations.management.state(cid)["deleted"] is True
        assert account.conversations.management.organization()["customOrder"] == []
        assert account.conversations.management.organization()["assignments"] == {}
        with pytest.raises(GatewayError, match="CONVERSATION_NOT_FOUND"):
            account.conversations.create("cc_0", "Thread 0", "cor_retry")
    finally:
        account.close()
    database.create_session(sid, "open_android")
    assert {row["conversationId"] for row in send(core, "GET", "/conversations")["data"]["conversations"]} == {untouched, failed}
    assert send(core, "GET", f"/conversations/{cid}/messages")["error"]["code"] == "CONVERSATION_NOT_FOUND"


def test_operation_fence_survives_unknown_native_outcome_and_only_same_request_can_retry(managed):
    core, database, _ = managed
    cid, _ = seed(core, database)[0]
    manager = core.conversation_manager
    def uncertain(*args):
        manager(*args)
        raise OSError("response lost after native commit")
    core.conversation_manager = uncertain
    assert send(core, "DELETE", f"/conversations/{cid}")["error"]["code"] == "OUTCOME_UNKNOWN"
    other = send(core, "POST", "/conversations", {"clientConversationId": "cc_after_unknown"}, "req_create_other")["data"]["conversation"]["conversationId"]
    assert send(core, "DELETE", f"/conversations/{other}")["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert send(core, "DELETE", f"/conversations/{cid}", device="dev_two")["error"]["code"] == "OUTCOME_UNKNOWN"
    assert send(core, "DELETE", f"/conversations/{cid}", request_id="req_other")["error"]["code"] == "OUTCOME_UNKNOWN"
    assert send(core, "PATCH", f"/conversations/{cid}", {"archived": True})["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    core.conversation_manager = manager
    assert send(core, "DELETE", f"/conversations/{cid}")["data"]["deleted"] is True


@pytest.mark.parametrize("operation", ["archive", "delete"])
def test_native_management_recovers_after_receipt_persistence_failure_and_restart(managed, monkeypatch, operation):
    core, database, store = managed
    cid, sid = seed(core, database)[0]
    calls = []
    setter, delete = database.set_session_archived, database.delete_sessions

    def archive_native(*args, **kwargs):
        calls.append("archive")
        return setter(*args, **kwargs)

    def delete_native(*args, **kwargs):
        calls.append("delete")
        return delete(*args, **kwargs)

    monkeypatch.setattr(database, "set_session_archived", archive_native)
    monkeypatch.setattr(database, "delete_sessions", delete_native)
    account = core.open_gateway_account("acct_one")
    try:
        account.store.database.execute("CREATE TRIGGER fail_management_receipt BEFORE INSERT ON idempotency_ledger WHEN NEW.request_id='req_receipt_loss' BEGIN SELECT RAISE(ABORT,'injected receipt persistence failure'); END")
    finally:
        account.close()
    method, body = ("PATCH", {"archived": True}) if operation == "archive" else ("DELETE", None)
    lost = send(core, method, f"/conversations/{cid}", body, "req_receipt_loss")
    assert lost["error"]["code"] == "OUTCOME_UNKNOWN"
    native_after_mutation = database.get_session(sid)
    if operation == "delete":
        assert native_after_mutation is None
    else:
        assert native_after_mutation["archived"] == 1
    assert calls == [operation]
    account = core.open_gateway_account("acct_one")
    try:
        assert account.conversations.management.state(cid)["pendingRequestId"] == "dev_one:req_receipt_loss"
        assert account.store.database.execute("SELECT 1 FROM idempotency_ledger WHERE request_id='req_receipt_loss'").fetchone() is None
        account.store.database.execute("DROP TRIGGER fail_management_receipt")
    finally:
        account.close()
    restarted = trust_core(create_gateway_core(core.storage_root, secret_store=core.secret_store))
    port = HermesHistoryPort(restarted, store)
    restarted.history_reader, restarted.conversation_manager = port.read, port.manage
    assert send(restarted, method, f"/conversations/{cid}", body, "req_new_key")["error"]["code"] == "OUTCOME_UNKNOWN"
    assert send(restarted, method, f"/conversations/{cid}", body, "req_receipt_loss", "dev_two")["error"]["code"] == "OUTCOME_UNKNOWN"
    assert send(restarted, "PATCH", f"/conversations/{cid}", {"archived": operation != "archive"}, "req_receipt_loss")["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    recovered = send(restarted, method, f"/conversations/{cid}", body, "req_receipt_loss")
    assert "error" not in recovered
    if operation == "delete":
        assert recovered["data"] == {"conversationId": cid, "deleted": True}
    else:
        assert recovered["data"]["conversation"]["archived"] is True
    assert database.get_session(sid) == native_after_mutation
    assert calls == [operation]
    assert send(restarted, method, f"/conversations/{cid}", body, "req_receipt_loss") == recovered
    account = restarted.open_gateway_account("acct_one")
    try:
        assert not account.conversations.management.state(cid).get("pending")
        assert account.store.database.execute("SELECT 1 FROM uncertain_outcomes WHERE request_id='req_receipt_loss'").fetchone() is None
        updates = [event for event in account.events.read_after(None) if event["eventType"] == "conversation.updated" and event["payload"]["conversationId"] == cid]
        assert len(updates) == 1
        # A COMMIT acknowledgement can also be lost after its receipt became
        # durable. The matching native receipt resolves that uncertainty too.
        account.store.database.execute("INSERT INTO uncertain_outcomes(device_id,request_id,input_hash,created_at,expires_at) "
            "SELECT device_id,request_id,input_hash,'2026-10-19T00:00:00.000Z',expires_at FROM idempotency_ledger WHERE request_id='req_receipt_loss'")
    finally:
        account.close()
    assert send(restarted, method, f"/conversations/{cid}", body, "req_receipt_loss") == recovered
    assert calls == [operation]


def test_signed_native_http_archive_delete_remove_the_real_route_and_cannot_recreate_it(native_store, tmp_path):
    from aiohttp import ClientSession
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from test_batch_http_dispatch import _adapter, _request, _signed_request
    from test_conversation_rename import ACCOUNT_ID, _gateway

    async def scenario():
        key = Ed25519PrivateKey.generate()
        core, bundle, _, exposure = _gateway(tmp_path, key)
        adapter = _adapter(core, exposure)
        adapter.set_session_store(native_store)
        assert await adapter.connect()
        base = f"http://127.0.0.1:{adapter._site._server.sockets[0].getsockname()[1]}"
        try:
            async with ClientSession() as client:
                created = await _request(client, base, bundle, key, "POST", PREFIX + "/conversations",
                    {"clientConversationId": "cc_native_management", "title": "Native management"}, "req_native_create")
                cid = created["data"]["conversation"]["conversationId"]
                account = core.open_gateway_account(ACCOUNT_ID)
                try:
                    binding = account.agent_sessions.lookup(cid)
                finally:
                    account.close()
                database = native_store._db_for_key(binding["sessionKey"])
                sid = binding["agentSessionId"]
                assert database.get_session(sid) is not None
                archived = await _request(client, base, bundle, key, "PATCH", PREFIX + f"/conversations/{cid}",
                    {"archived": True}, "req_native_archive")
                assert archived["data"]["conversation"]["archived"] is True
                assert database.get_session(sid)["archived"] == 1
                deleted = await _request(client, base, bundle, key, "DELETE", PREFIX + f"/conversations/{cid}",
                    None, "req_native_delete")
                assert deleted["data"] == {"conversationId": cid, "deleted": True}
                assert database.get_session(sid) is None
                assert native_store.lookup_by_session_key(binding["sessionKey"]) is None
                target = PREFIX + f"/conversations/{cid}/messages"
                _, headers = _signed_request(bundle, key, "GET", target, None, "req_native_deleted_history")
                async with client.get(base + target, headers=headers) as response:
                    assert response.status == 404
                    assert (await response.json())["error"]["code"] == "CONVERSATION_NOT_FOUND"
                assert native_store.lookup_by_session_key(binding["sessionKey"]) is None
        finally:
            await adapter.disconnect()
    asyncio.run(scenario())
