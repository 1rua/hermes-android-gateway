"""Legacy duplicate client identities must never select an arbitrary thread."""
import json

import pytest

from open_android_intelligence_gateway.core import GatewayError, _hash_input, create_gateway_core
from test_support import make_secret_store, trust_core


CLIENT = "cc_legacy"
TITLE = "Original title"
TARGET = "/open-android-intelligence/v2/conversations"
NOW = "2026-10-16T00:00:00.000Z"


@pytest.fixture
def account(tmp_path):
    core = trust_core(create_gateway_core(tmp_path, secret_store=make_secret_store()))
    account = core.open_gateway_account("acct_legacy")
    yield core, account
    account.close()


def _seed(account, conversation_id, title=TITLE):
    account.store.database.execute(
        "INSERT INTO conversations VALUES (?, ?, ?, ?)",
        (conversation_id, CLIENT, title, NOW),
    )
    account.agent_sessions.record(conversation_id, "conversation-create", "req_seed", NOW)
    account.agent_sessions.attach(conversation_id, "native_" + conversation_id)
    account.store.database.execute(
        "INSERT INTO messages(message_id,conversation_id,client_message_id,text,created_at,attachment_ids_json) "
        "VALUES (?,?,?,?,?, '[]')",
        ("msg_" + conversation_id, conversation_id, "cm_" + conversation_id, "history", NOW),
    )


def _metadata(account, value):
    account.store.database.execute(
        "INSERT OR REPLACE INTO account_metadata VALUES (?, ?)",
        ("conversation-create:" + CLIENT, json.dumps(value)),
    )


def _receipt(account, conversation_id, request_id="req_original", title=TITLE, input_hash=None):
    outcome = {
        "requestId": request_id, "correlationId": "cor_original", "protocol": "2.1",
        "data": {"conversation": {
            "conversationId": conversation_id, "clientConversationId": CLIENT, "title": title,
        }},
    }
    account.store.database.execute(
        "INSERT INTO idempotency_ledger VALUES (?, ?, ?, ?, ?)",
        ("dev_legacy", request_id,
         input_hash or _hash_input("POST", TARGET, {"clientConversationId": CLIENT, "title": title}),
         account.store.seal_json(outcome, "idempotency:dev_legacy:" + request_id),
         "2999-01-01T00:00:00.000Z"),
    )
    return outcome


def _preserved(account):
    return {
        table: [tuple(row) for row in account.store.database.execute("SELECT * FROM " + table)]
        for table in ("conversations", "messages", "conversation_agent_sessions", "idempotency_ledger")
    }


def _assert_conflict(account):
    before = _preserved(account)
    with pytest.raises(GatewayError, match="IDEMPOTENCY_CONFLICT"):
        account.conversations.create(CLIENT, TITLE, "cor_retry", NOW)
    assert _preserved(account) == before


@pytest.mark.parametrize("with_old_metadata", [False, True])
def test_legacy_duplicate_without_unique_receipt_is_refused(account, with_old_metadata):
    _, account = account
    _seed(account, "conv_first")
    _seed(account, "conv_second")
    if with_old_metadata:
        _metadata(account, {"title": TITLE})
    _assert_conflict(account)


@pytest.mark.parametrize("with_old_metadata", [False, True])
def test_unique_authenticated_create_receipt_locks_second_thread_and_preserves_all_history(account, with_old_metadata):
    core, account = account
    _seed(account, "conv_first")
    _seed(account, "conv_canonical", "Renamed")
    if with_old_metadata:
        _metadata(account, {"title": TITLE})
    original = _receipt(account, "conv_canonical")
    before = _preserved(account)

    result = account.conversations.create(CLIENT, TITLE, "cor_retry", NOW)

    assert result == original["data"]["conversation"]
    assert _preserved(account) == before
    receipt = _canonical(account)
    assert receipt == {"conversationId": "conv_canonical", "title": TITLE}
    # The identity remains fixed after the historical HTTP evidence is pruned.
    account.store.database.execute("DELETE FROM idempotency_ledger")
    assert account.conversations.create(CLIENT, TITLE, "cor_later", NOW) == result
    account.store.database.execute(
        "INSERT INTO idempotency_ledger VALUES (?,?,?,?,?)", before["idempotency_ledger"][0],
    )
    replay = core.handle({
        "method": "POST", "target": TARGET,
        "context": {"accountId": account.account_id, "deviceId": "dev_legacy", "sessionId": "sess_legacy",
                    "requestId": "req_original", "correlationId": "cor_retry_http",
                    "pairingGeneration": 1, "grantRevision": 1},
        "body": {"clientConversationId": CLIENT, "title": TITLE},
        "idempotencyKey": "req_original", "now": NOW,
    })
    assert replay == original
    assert _preserved(account) == before
    with pytest.raises(GatewayError, match="IDEMPOTENCY_CONFLICT"):
        account.conversations.create(CLIENT, "Changed", "cor_conflict", NOW)


def test_conflicting_successful_receipts_are_not_resolved_by_row_order(account):
    _, account = account
    _seed(account, "conv_first")
    _seed(account, "conv_second")
    _receipt(account, "conv_first", "req_first")
    _receipt(account, "conv_second", "req_second")
    _assert_conflict(account)


@pytest.mark.parametrize("bad_hash", [True, False])
def test_unrelated_or_missing_conversation_receipt_cannot_establish_canonical(account, bad_hash):
    _, account = account
    _seed(account, "conv_first")
    _seed(account, "conv_second")
    _receipt(account, "conv_first" if bad_hash else "conv_missing",
             input_hash="wrong-input-hash" if bad_hash else None)
    _assert_conflict(account)


@pytest.mark.parametrize("metadata", [None, {"title": TITLE}])
def test_single_legacy_thread_is_lazily_locked_without_changing_native_binding(account, metadata):
    _, account = account
    _seed(account, "conv_only")
    if metadata is not None:
        _metadata(account, metadata)
    before = _preserved(account)
    result = account.conversations.create(CLIENT, TITLE, "cor_retry", NOW)
    assert result["conversationId"] == "conv_only"
    stored = _canonical(account)
    assert stored == {"conversationId": "conv_only", "title": TITLE}
    _seed(account, "conv_later_duplicate")
    assert account.conversations.create(CLIENT, TITLE, "cor_later", NOW) == result
    assert _preserved(account)["conversation_agent_sessions"][0] == before["conversation_agent_sessions"][0]


def test_new_creation_persists_canonical_identity_and_replays_original_title_after_rename(account):
    _, account = account
    first = account.conversations.create(CLIENT, TITLE, "cor_create", NOW)
    account.conversations.update_title(first["conversationId"], "Renamed", "cor_rename", NOW)
    _seed(account, "conv_later_duplicate")
    assert account.conversations.create(CLIENT, TITLE, "cor_retry", NOW) == first
    stored = _canonical(account)
    assert stored == {"conversationId": first["conversationId"], "title": TITLE}


def test_stale_canonical_mapping_is_refused_instead_of_selecting_remaining_row(account):
    _, account = account
    _seed(account, "conv_first")
    _metadata(account, {"conversationId": "conv_missing", "title": TITLE})
    _assert_conflict(account)


def test_receipts_disagreeing_about_original_title_cannot_lock_identity(account):
    _, account = account
    _seed(account, "conv_first")
    _seed(account, "conv_second")
    _receipt(account, "conv_second", "req_first")
    _receipt(account, "conv_second", "req_second", title="Different original")
    _assert_conflict(account)


def test_legacy_title_metadata_must_agree_with_reliable_http_receipt(account):
    _, account = account
    _seed(account, "conv_first")
    _seed(account, "conv_second")
    _metadata(account, {"title": "Different original"})
    _receipt(account, "conv_second")
    _assert_conflict(account)


@pytest.mark.parametrize("omit_title", [False, True])
def test_expired_receipts_remain_historical_evidence_without_changing_http_replay(account, omit_title):
    core, account = account
    _seed(account, "conv_first")
    _seed(account, "conv_second", "Renamed")
    body = {"clientConversationId": CLIENT}
    if not omit_title:
        body["title"] = None
    _receipt(account, "conv_second", title=None, input_hash=_hash_input("POST", TARGET, body))
    account.store.database.execute("UPDATE idempotency_ledger SET expires_at='2020-01-01T00:00:00.000Z'")
    before = _preserved(account)
    assert account.conversations.create(CLIENT, None, "cor_retry", NOW)["conversationId"] == "conv_second"
    replay = core.handle({
        "method": "POST", "target": TARGET,
        "context": {"accountId": account.account_id, "deviceId": "dev_legacy", "sessionId": "sess_legacy",
                    "requestId": "req_original", "correlationId": "cor_retry_http",
                    "pairingGeneration": 1, "grantRevision": 1},
        "body": body, "idempotencyKey": "req_original", "now": NOW,
    })
    assert replay["error"]["code"] == "OUTCOME_UNKNOWN"
    assert _preserved(account) == before


def test_unreadable_http_evidence_refuses_duplicate_identity(account):
    _, account = account
    _seed(account, "conv_first")
    _seed(account, "conv_second")
    _receipt(account, "conv_second")
    account.store.database.execute("UPDATE idempotency_ledger SET outcome_json='unreadable'")
    _assert_conflict(account)


@pytest.mark.parametrize("metadata", [None, {}, {"title": []}, {"title": TITLE, "conversationId": []}])
def test_invalid_canonical_metadata_is_refused_explicitly(account, metadata):
    _, account = account
    _seed(account, "conv_first")
    _metadata(account, metadata)
    _assert_conflict(account)


def _canonical(account):
    key = "conversation-create:" + CLIENT
    raw = account.store.database.execute(
        "SELECT value FROM account_metadata WHERE key=?", (key,),
    ).fetchone()[0]
    assert raw.startswith("aead-v1:"), "Canonical receipts must encrypt the original user title"
    assert TITLE not in raw
    return account.store.open_json(raw, key)


@pytest.mark.parametrize("mode", ["new", "single_legacy", "duplicate_legacy"])
def test_canonical_receipt_is_encrypted_and_stable_after_reopening(account, mode):
    core, account = account
    if mode != "new":
        _seed(account, "conv_canonical")
        _metadata(account, {"title": TITLE})
    if mode == "duplicate_legacy":
        _seed(account, "conv_duplicate")
        _receipt(account, "conv_canonical")
    result = account.conversations.create(CLIENT, TITLE, "cor_create", NOW)
    assert _canonical(account) == {"conversationId": result["conversationId"], "title": TITLE}
    account.conversations.update_title(result["conversationId"], "Renamed", "cor_rename", NOW)
    account.close()
    reopened = core.open_gateway_account(account.account_id)
    try:
        assert reopened.conversations.create(CLIENT, TITLE, "cor_reopen", NOW) == result
        assert _canonical(reopened) == {"conversationId": result["conversationId"], "title": TITLE}
    finally:
        reopened.close()


@pytest.mark.parametrize("corruption", ["malformed", "tampered", "wrong_purpose"])
def test_encrypted_canonical_receipt_rejects_corruption_and_other_identity_binding(account, corruption):
    _, account = account
    result = account.conversations.create(CLIENT, TITLE, "cor_create", NOW)
    key = "conversation-create:" + CLIENT
    value = {"conversationId": result["conversationId"], "title": TITLE}
    if corruption == "malformed":
        raw = "aead-v1:invalid"
    elif corruption == "wrong_purpose":
        raw = account.store.seal_json(value, "conversation-create:another-client")
    else:
        raw = account.store.seal_json(value, key)
        offset = len("aead-v1:") + 5
        raw = raw[:offset] + ("A" if raw[offset] != "A" else "B") + raw[offset + 1:]
    account.store.database.execute("UPDATE account_metadata SET value=? WHERE key=?", (raw, key))
    _assert_conflict(account)
