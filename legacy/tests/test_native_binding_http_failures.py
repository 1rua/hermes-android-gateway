"""Signed create retries retain their receipt and require a resolvable native binding."""
import asyncio

import pytest
from aiohttp import ClientSession
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from test_native_turn_lifecycle import native_store
from test_batch_http_dispatch import PREFIX, _adapter, _request, _signed_request
from test_conversation_rename import ACCOUNT_ID, _gateway
from open_android_intelligence_gateway.platform_identity import GATEWAY_PLATFORM_IDS


@pytest.mark.parametrize("broken", ["missing-bound-session", "ambiguous-legacy-sessions"])
def test_create_binding_error_is_structured_and_same_receipt_recovers(native_store, tmp_path, monkeypatch, broken):
    async def scenario():
        key = Ed25519PrivateKey.generate()
        core, bundle, cid, exposure = _gateway(tmp_path, key)
        adapter = _adapter(core, exposure)
        adapter.set_session_store(native_store)
        assert await adapter.connect()
        base = f"http://127.0.0.1:{adapter._site._server.sockets[0].getsockname()[1]}"
        target = PREFIX + "/conversations"
        body = {"clientConversationId": "cconv_rename", "title": "原标题"}
        request_id = "req_binding_retry"
        lookup = native_store.lookup_by_session_key
        get_or_create = native_store.get_or_create_session
        creates = []
        def counted(*args, **kwargs):
            creates.append(1)
            return get_or_create(*args, **kwargs)
        monkeypatch.setattr(native_store, "get_or_create_session", counted)
        try:
            async with ClientSession() as client:
                if broken == "missing-bound-session":
                    original = await _request(client, base, bundle, key, "POST", target, body, request_id)
                    account = core.open_gateway_account(ACCOUNT_ID)
                    try:
                        bound_key = account.agent_sessions.lookup(cid)["sessionKey"]
                    finally:
                        account.close()
                    monkeypatch.setattr(native_store, "lookup_by_session_key", lambda key: None if key == bound_key else lookup(key))
                    reason = "UNRESOLVED_AGENT_SESSION_BINDING"
                else:
                    entries = [get_or_create(adapter._agent_source(cid, ACCOUNT_ID, platform_id)) for platform_id in tuple(GATEWAY_PLATFORM_IDS)[:2]]
                    assert len(entries) == 2
                    reason = "AMBIGUOUS_AGENT_SESSION"
                before_creates = len(creates)
                recorded = None
                for _ in range(2):
                    content, headers = _signed_request(bundle, key, "POST", target, body, request_id)
                    async with client.post(base + target, data=content, headers=headers) as response:
                        assert response.status == 503
                        failure = await response.json()
                    assert failure["requestId"] == request_id
                    assert failure["error"]["code"] == "HOST_INCOMPATIBLE"
                    assert failure["error"]["retryable"] is False
                    assert failure["error"]["details"] == {
                        "reason": reason, "conversationId": cid, "conversationCreated": True,
                        "recovery": "REPAIR_HOST_SESSION_BINDING_AND_RETRY_SAME_REQUEST",
                    }
                    account = core.open_gateway_account(ACCOUNT_ID)
                    try:
                        ledger = tuple(account.store.database.execute("SELECT * FROM idempotency_ledger WHERE request_id=?", (request_id,)).fetchone())
                        assert account.store.database.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
                    finally:
                        account.close()
                    if recorded is not None:
                        assert ledger == recorded
                    recorded = ledger
                assert len(creates) == before_creates
                # Restoring the exact bound route, or explicitly resolving the
                # ambiguous legacy route, makes the original HTTP receipt usable.
                monkeypatch.setattr(native_store, "lookup_by_session_key", lookup if broken == "missing-bound-session" else
                    lambda key: None if key == entries[1].session_key else lookup(key))
                recovered = await _request(client, base, bundle, key, "POST", target, body, request_id)
                assert recovered["data"]["conversation"]["conversationId"] == cid
                if broken == "missing-bound-session":
                    assert recovered == original
                account = core.open_gateway_account(ACCOUNT_ID)
                try:
                    assert tuple(account.store.database.execute("SELECT * FROM idempotency_ledger WHERE request_id=?", (request_id,)).fetchone()) == recorded
                    binding = account.agent_sessions.lookup(cid)
                    assert binding["agentSessionId"] == lookup(binding["sessionKey"]).session_id
                finally:
                    account.close()
                assert len(creates) == before_creates
        finally:
            await adapter.disconnect()
    asyncio.run(scenario())


def test_new_signed_create_identity_recovers_legacy_canonical_without_switching_native_session(native_store, tmp_path):
    async def scenario():
        key = Ed25519PrivateKey.generate()
        core, bundle, cid, exposure = _gateway(tmp_path, key)
        adapter = _adapter(core, exposure)
        adapter.set_session_store(native_store)
        assert await adapter.connect()
        base = f"http://127.0.0.1:{adapter._site._server.sockets[0].getsockname()[1]}"
        target = PREFIX + "/conversations"
        body = {"clientConversationId": "cconv_rename", "title": "原标题"}
        try:
            async with ClientSession() as client:
                original = await _request(client, base, bundle, key, "POST", target, body, "req_canonical_original")
                account = core.open_gateway_account(ACCOUNT_ID)
                try:
                    original_binding = account.agent_sessions.lookup(cid)
                    original_receipt = tuple(account.store.database.execute(
                        "SELECT * FROM idempotency_ledger WHERE request_id='req_canonical_original'",
                    ).fetchone())
                    account.store.database.execute(
                        "INSERT INTO conversations SELECT 'conv_legacy_duplicate',client_conversation_id,'Different history',created_at FROM conversations WHERE conversation_id=?", (cid,),
                    )
                    # Reproduce a legacy database whose arbitrary first row is
                    # a different timeline from the successful HTTP receipt.
                    account.store.database.execute("UPDATE conversations SET rowid=rowid+1000 WHERE conversation_id=?", (cid,))
                    account.store.database.execute("UPDATE account_metadata SET value=? WHERE key=?",
                        ('{"title":"原标题"}', "conversation-create:cconv_rename"))
                    assert account.store.database.execute("SELECT conversation_id FROM conversations WHERE client_conversation_id=?",
                        ("cconv_rename",)).fetchone()[0] == "conv_legacy_duplicate"
                finally:
                    account.close()
                await adapter._ensure_agent_session("conv_legacy_duplicate", ACCOUNT_ID,
                    force_new=False, created_via="conversation-create")
                sessions = {entry.session_id for entry in native_store.list_sessions()}
                recovered = await _request(client, base, bundle, key, "POST", target, body, "req_canonical_new")
                assert recovered["data"] == original["data"]
                assert await _request(client, base, bundle, key, "POST", target, body, "req_canonical_original") == original
                assert {entry.session_id for entry in native_store.list_sessions()} == sessions
                account = core.open_gateway_account(ACCOUNT_ID)
                try:
                    assert account.agent_sessions.lookup(cid) == original_binding
                    assert account.agent_sessions.lookup("conv_legacy_duplicate")["agentSessionId"] != original_binding["agentSessionId"]
                    assert account.store.database.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 2
                    assert tuple(account.store.database.execute(
                        "SELECT * FROM idempotency_ledger WHERE request_id='req_canonical_original'",
                    ).fetchone()) == original_receipt
                finally:
                    account.close()
        finally:
            await adapter.disconnect()
    asyncio.run(scenario())
