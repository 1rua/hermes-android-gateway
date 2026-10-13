"""会话重新打开时只能展示原始发送内容，不能展示供模型使用的加工输入。"""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import asyncio
import hashlib
import json
import shutil
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from open_android_intelligence_gateway.core import create_gateway_core
from open_android_intelligence_gateway.host_history import HermesHistoryPort
from open_android_intelligence_gateway.history_media import HistoryMedia
from test_support import make_secret_store


def test_native_display_metadata_does_not_change_real_compaction_tail(tmp_path):
    SessionDB = pytest.importorskip("hermes_state").SessionDB
    database = SessionDB(tmp_path / "native.db")
    core = create_gateway_core(tmp_path / "gateway", secret_store=make_secret_store())
    port = HermesHistoryPort(core, SimpleNamespace(_db=database))
    core.history_reader = port.read
    account = core.open_gateway_account("acct_tail")
    try:
        conversation = account.conversations.create("cc_tail", "合成压缩尾部", "cor_tail")["conversationId"]
        database.create_session("native_tail", "open_android")
        account.agent_sessions.record(conversation, "inbound-message")
        assert account.agent_sessions.attach(conversation, "native_tail")
        accepted = account.conversations.accept_message(conversation, "cm_tail", "原始合成提问", [],
            "dev_tail", "req_tail", "cor_tail")
        dispatch = account.conversations.claim_dispatch("cm_tail")
        port.capture_submission(account, dispatch)
        database.append_message("native_tail", "user", "模型加工后的合成提问", platform_message_id="cm_tail", timestamp=1)
        database.append_message("native_tail", "assistant", "合成最终回复", timestamp=2)
        port.record_visible(account, {"messageId":"msg_tail_final", "conversationId":conversation,
            "sender":"assistant", "text":"合成最终回复", "parts":[{"type":"text", "text":"合成最终回复"}],
            "timestamp":2000, "anchorClientMessageId":"cm_tail"})
        account.conversations.update_dispatch_status(accepted["messageId"], "delivered", None, "cor_tail", claim_token=dispatch["claimToken"])
        before = account.conversations.list_messages(conversation)["messages"]
        assert len(database.get_messages("native_tail")) == 2, "展示信息不得进入模型 transcript 的物理尾部"
        tail = [{key:row[key] for key in ("role","content","timestamp","platform_message_id") if row.get(key) is not None}
                for row in database.get_messages("native_tail")]
        database.archive_and_compact("native_tail", [{"role":"user", "content":"内部压缩摘要", "_compressed_summary":True}, *tail], tail_count=2)
        after = account.conversations.list_messages(conversation)["messages"]
        assert {row["messageId"]:row["parts"] for row in after} == {row["messageId"]:row["parts"] for row in before}
        assert all(row["state"] != "DELETED" for row in after)
        assert next(row for row in after if row["messageId"] == accepted["messageId"])["text"] == "原始合成提问"
    finally:
        account.close()
        database.close()


def test_native_legacy_display_rows_migrate_once_without_changing_model_rows(tmp_path):
    SessionDB = pytest.importorskip("hermes_state").SessionDB
    from open_android_intelligence_gateway.host_history import PRESENTATION_KIND, PRESENTATION_KEY
    database = SessionDB(tmp_path / "native.db")
    core = create_gateway_core(tmp_path / "gateway", secret_store=make_secret_store())
    port = HermesHistoryPort(core, SimpleNamespace(_db=database))
    core.history_reader = port.read
    account = core.open_gateway_account("acct_migrate")
    try:
        conversation = account.conversations.create("cc_migrate", "合成展示迁移", "cor_migrate")["conversationId"]
        database.create_session("native_migrate", "open_android")
        account.agent_sessions.record(conversation, "inbound-message")
        assert account.agent_sessions.attach(conversation, "native_migrate")
        database.append_message("native_migrate", "assistant", "模型原始回复", timestamp=2)
        model = database.get_messages("native_migrate")
        snapshot = {"version":1, "storageOwner":port.storage_owner, "accountId":"acct_migrate",
            "conversationId":conversation, "messageId":"msg_migrate", "sender":"assistant", "clientMessageId":None,
            "text":"已发布的独立工具提示", "parts":[{"type":"text", "text":"已发布的独立工具提示"}],
            "timestamp":1000, "state":"CONFIRMED", "revision":3}
        database.append_message("native_migrate", "session_meta", "legacy_marker", display_kind=PRESENTATION_KIND,
            display_metadata={PRESENTATION_KEY:snapshot}, timestamp=1)
        first = account.conversations.list_messages(conversation)["messages"]
        shown = next(row for row in first if row["messageId"] == "msg_migrate")
        assert shown["text"] == "已发布的独立工具提示" and shown["timestamp"] == 1000 and shown["contentRevision"] == 3
        assert database.get_messages("native_migrate") == model
        second = account.conversations.list_messages(conversation)["messages"]
        assert second == first
        assert len(database.list_meta_prefix("oai_gateway_presentation_v2:")) == 1
    finally:
        account.close()
        database.close()


def test_native_equal_notice_never_claims_an_unrelated_old_reply(tmp_path):
    SessionDB = pytest.importorskip("hermes_state").SessionDB
    database = SessionDB(tmp_path / "native.db")
    core = create_gateway_core(tmp_path / "gateway", secret_store=make_secret_store())
    port = HermesHistoryPort(core, SimpleNamespace(_db=database))
    core.history_reader = port.read
    account = core.open_gateway_account("acct_equal")
    try:
        conversation = account.conversations.create("cc_equal", "合成同文通知", "cor_equal")["conversationId"]
        database.create_session("native_equal", "open_android")
        account.agent_sessions.record(conversation, "inbound-message")
        assert account.agent_sessions.attach(conversation, "native_equal")
        database.append_message("native_equal", "assistant", "相同合成回答", timestamp=1)
        port.record_visible(account, {"messageId":"msg_new_notice", "conversationId":conversation,
            "sender":"assistant", "text":"相同合成回答", "parts":[{"type":"text", "text":"相同合成回答"}], "timestamp":2000}, "STREAMING")
        rows = account.conversations.list_messages(conversation)["messages"]
        old = next(row for row in rows if row["timestamp"] == 1000)
        assert old["messageId"] != "msg_new_notice"
        assert next(row for row in rows if row["messageId"] == "msg_new_notice")["timestamp"] == 2000
    finally:
        account.close()
        database.close()


def test_native_profile_rotation_cleans_the_owning_metadata_database(tmp_path, monkeypatch):
    pytest.importorskip("hermes_state")
    from gateway.config import GatewayConfig
    from gateway.session import SessionEntry, SessionStore
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_TEST_ISOLATION", str(home))
    store = SessionStore(home / "sessions", GatewayConfig(multiplex_profiles=True))
    profile = home / "profiles" / "projection"
    profile.mkdir(parents=True)
    store._profile_home_cache["projection"] = profile
    key = "agent:projection:open_android:dm:projection-chat"
    now = datetime.now(timezone.utc)
    store._entries[key] = SessionEntry(session_key=key, session_id="native_parent", created_at=now, updated_at=now)
    database = store._db_for_session_id("native_parent")
    core = create_gateway_core(tmp_path / "gateway", secret_store=make_secret_store())
    port = HermesHistoryPort(core, store)
    account = core.open_gateway_account("acct_profile")
    try:
        conversation = account.conversations.create("cc_profile", "合成所属数据库", "cor_profile")["conversationId"]
        database.create_session("native_parent", "open_android", session_key=key)
        account.agent_sessions.record(conversation, "inbound-message")
        assert account.agent_sessions.attach(conversation, "native_parent", key)
        port.record_visible(account, {"messageId":"msg_profile", "conversationId":conversation,
            "sender":"assistant", "text":"合成原文", "parts":[{"type":"text", "text":"合成原文"}], "timestamp":1000})
        database.end_session("native_parent", "compression")
        database.create_session("native_child", "open_android", parent_session_id="native_parent", session_key=key)
        store._publish_transcript_reroute("native_parent", "native_child")
        assert store._db_for_session_id("native_parent") is not database
        port.cleanup_account("acct_profile")
        assert not any(json.loads(value).get("accountId") == "acct_profile"
                       for _, value in database.list_meta_prefix("oai_gateway_presentation_v2:"))
        assert database.get_session("native_child") is not None
    finally:
        account.close()
        store.close_all_db_handles()


def test_native_explicit_same_identity_edit_updates_original_once(tmp_path):
    SessionDB = pytest.importorskip("hermes_state").SessionDB
    database = SessionDB(tmp_path / "native.db")
    core = create_gateway_core(tmp_path / "gateway", secret_store=make_secret_store())
    port = HermesHistoryPort(core, SimpleNamespace(_db=database))
    core.history_reader = port.read
    account = core.open_gateway_account("acct_edit")
    try:
        conversation = account.conversations.create("cc_edit", "合成明确编辑", "cor_edit")["conversationId"]
        database.create_session("native_edit", "open_android")
        account.agent_sessions.record(conversation, "inbound-message")
        assert account.agent_sessions.attach(conversation, "native_edit")
        accepted = account.conversations.accept_message(conversation, "cm_edit", "原始合成提问", [],
            "dev_edit", "req_edit", "cor_edit")
        dispatch = account.conversations.claim_dispatch("cm_edit")
        port.capture_submission(account, dispatch)
        database.append_message("native_edit", "user", "模型加工后的原始提问", platform_message_id="cm_edit", timestamp=1)
        account.conversations.update_dispatch_status(accepted["messageId"], "delivered", None, "cor_edit", claim_token=dispatch["claimToken"])
        before = account.conversations.list_messages(conversation)["messages"]
        assert next(row for row in before if row["messageId"] == accepted["messageId"])["text"] == "原始合成提问"
        kept = database.get_messages_as_conversation("native_edit")
        kept[0]["content"] = "用户在宿主明确编辑的新提问"
        database.replace_messages("native_edit", kept, archive_dropped=True)
        after = account.conversations.list_messages(conversation)["messages"]
        assert next(row for row in after if row["messageId"] == accepted["messageId"])["text"] == "用户在宿主明确编辑的新提问"
        count = len([event for event in account.events.read_after(None) if event["eventType"] == "conversation.message.completed"])
        account.conversations.list_messages(conversation)
        assert len([event for event in account.events.read_after(None) if event["eventType"] == "conversation.message.completed"]) == count
    finally:
        account.close()
        database.close()


@pytest.mark.parametrize("entry", ["metadata", "grant", "content"])
def test_native_asset_withdrawal_is_checked_at_each_entry_before_timeline(tmp_path, monkeypatch, entry):
    SessionDB = pytest.importorskip("hermes_state").SessionDB
    from open_android_intelligence_gateway.core import GatewayError
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_TEST_ISOLATION", str(home))
    output = home / "outputs" / "original.txt"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"synthetic original")
    database = SessionDB(tmp_path / "native.db")
    core = create_gateway_core(tmp_path / "gateway", secret_store=make_secret_store())
    port = HermesHistoryPort(core, SimpleNamespace(_db=database))
    core.history_reader = port.read
    account = core.open_gateway_account("acct_asset")
    context = {"deviceId":"dev_asset", "pairingGeneration":1, "grantRevision":1}
    try:
        conversation = account.conversations.create("cc_asset", "合成原件撤回", "cor_asset")["conversationId"]
        database.create_session("native_asset", "open_android")
        account.agent_sessions.record(conversation, "inbound-message")
        assert account.agent_sessions.attach(conversation, "native_asset")
        database.append_message("native_asset", "assistant", "合成原件说明", platform_message_id="msg_asset", timestamp=1)
        account.conversations.record_assistant_message(conversation, "msg_asset", "合成原件说明")
        media = HistoryMedia(account)
        part = media.register(conversation, "msg_asset", output)
        port.record_visible(account, {"messageId":"msg_asset", "conversationId":conversation,
            "sender":"assistant", "text":"合成原件说明", "parts":[{"type":"text", "text":"合成原件说明"}, part], "timestamp":1000})
        token = media.grant(conversation, part["attachmentId"], context)["grantId"]
        database.replace_messages("native_asset", [], archive_dropped=True)
        if entry == "metadata":
            assert not media.metadata(conversation, part["attachmentId"])["remoteAvailable"]
        else:
            with pytest.raises(GatewayError) as rejected:
                if entry == "grant":
                    media.grant(conversation, part["attachmentId"], context)
                else:
                    media.content(conversation, part["attachmentId"], token, context)
            assert rejected.value.code == "ATTACHMENT_NOT_FOUND"
    finally:
        account.close()
        database.close()


def test_native_model_image_enrichment_does_not_replace_original_user_text(tmp_path):
    SessionDB = pytest.importorskip("hermes_state").SessionDB
    database = SessionDB(tmp_path / "native.db")
    core = create_gateway_core(tmp_path / "gateway", secret_store=make_secret_store())
    core.history_reader = HermesHistoryPort(core, SimpleNamespace(_db=database)).read
    account = core.open_gateway_account("acct_projection")
    now = datetime.now(timezone.utc)
    try:
        conversation = account.conversations.create("cc_projection", "图片对话", "cor_create")["conversationId"]
        database.create_session("native_projection", "open_android")
        account.agent_sessions.record(conversation, "inbound-message", now=now)
        assert account.agent_sessions.attach(conversation, "native_projection")
        accepted = account.conversations.accept_message(
            conversation, "cm_projection", "请说明这张图片", [], "dev_projection", "req_projection", "cor_projection", now=now,
        )
        dispatch = account.conversations.claim_dispatch("cm_projection", now=now)
        account.conversations.update_dispatch_status(
            accepted["messageId"], "delivered", None, "cor_projection", now=now, claim_token=dispatch["claimToken"],
        )
        account.conversations.update_dispatch_status(accepted["messageId"], "completed", None, "cor_projection", now=now)
        database.append_message(
            "native_projection", "user",
            "[模型专用合成图片描述]\n[模型专用合成宿主媒体引用]\n\n请说明这张图片",
            platform_message_id="cm_projection", timestamp=now.timestamp(),
        )
        database.append_message("native_projection", "assistant", "这是一张合成测试图片", timestamp=now.timestamp() + 1)
    finally:
        account.close()
    try:
        reopened = core.open_gateway_account("acct_projection")
        try:
            user = next(row for row in reopened.conversations.list_messages(conversation)["messages"] if row["sender"] == "user")
            assert user["messageId"] == accepted["messageId"]
            assert user["clientMessageId"] == "cm_projection"
            assert user["text"] == "请说明这张图片"
            assert user["parts"] == [{"type": "text", "text": "请说明这张图片"}]
        finally:
            reopened.close()
    finally:
        database.close()


def test_native_history_keeps_the_attachment_identity_and_metadata_of_the_user_message(tmp_path):
    SessionDB = pytest.importorskip("hermes_state").SessionDB
    database = SessionDB(tmp_path / "native.db")
    core = create_gateway_core(tmp_path / "gateway", secret_store=make_secret_store())
    account = core.open_gateway_account("acct_projection")
    now = datetime.now(timezone.utc)
    try:
        conversation = account.conversations.create("cc_media_projection", "图片对话", "cor_create")["conversationId"]
        body = b"synthetic image bytes"
        attachment = account.attachments.create(
            clientAttachmentId="ca_projection", filename="synthetic.png", mediaType="image/png",
            sizeBytes=len(body), sha256=hashlib.sha256(body).hexdigest(), correlationId="cor_media", now=now,
        )
        attachment_id = attachment["attachmentId"]
        account.attachments.upload_content(attachment_id, body, now=now)
        account.attachments.commit(attachment_id, now=now)
        accepted = account.conversations.accept_message(
            conversation, "cm_media_projection", "请说明这张图片", [attachment_id],
            "dev_projection", "req_media_projection", "cor_media_projection", now=now,
        )
        expected_attachment = {
            "type": "attachment", "attachmentId": attachment_id,
            "filename": "synthetic.png", "mediaType": "image/png", "sizeBytes": len(body),
        }
        before = account.conversations.list_messages(conversation)["messages"]
        assert expected_attachment in before[0]["parts"]
        database.create_session("native_projection", "open_android")
        account.agent_sessions.record(conversation, "inbound-message", now=now)
        assert account.agent_sessions.attach(conversation, "native_projection")
        dispatch = account.conversations.claim_dispatch("cm_media_projection", now=now)
        account.conversations.update_dispatch_status(
            accepted["messageId"], "delivered", None, "cor_media_projection", now=now, claim_token=dispatch["claimToken"],
        )
        account.conversations.update_dispatch_status(accepted["messageId"], "completed", None, "cor_media_projection", now=now)
        database.append_message(
            "native_projection", "user", "请说明这张图片",
            platform_message_id="cm_media_projection", timestamp=now.timestamp(),
        )
    finally:
        account.close()
    core.history_reader = HermesHistoryPort(core, SimpleNamespace(_db=database)).read
    try:
        reopened = core.open_gateway_account("acct_projection")
        try:
            after = reopened.conversations.list_messages(conversation)["messages"]
            user = next(row for row in after if row["messageId"] == accepted["messageId"])
            assert user["text"] == "请说明这张图片"
            assert expected_attachment in user["parts"]
        finally:
            reopened.close()
    finally:
        database.close()


def test_native_tool_call_history_keeps_published_progress_and_has_no_empty_assistant_bubble(tmp_path):
    SessionDB = pytest.importorskip("hermes_state").SessionDB
    from test_assistant_message_identity import _adapter, ACCOUNT_ID
    from gateway.platform_registry import PlatformEntry, platform_registry
    from open_android_intelligence_gateway.adapter import OpenAndroidPlatformAdapter
    from open_android_intelligence_gateway.platform_identity import GATEWAY_PLATFORM_ID, GATEWAY_PLATFORM_IDS

    database = SessionDB(tmp_path / "native.db")
    core = create_gateway_core(tmp_path / "gateway", secret_store=make_secret_store())
    core.history_reader = HermesHistoryPort(core, SimpleNamespace(_db=database)).read
    account = core.open_gateway_account(ACCOUNT_ID)
    now = datetime.now(timezone.utc)
    try:
        conversation = account.conversations.create("cc_tool_projection", "工具对话", "cor_create")["conversationId"]
        database.create_session("native_projection", "open_android")
        account.agent_sessions.record(conversation, "inbound-message", now=now)
        assert account.agent_sessions.attach(conversation, "native_projection")
        database.append_message("native_projection", "user", "查询合成测试信息", timestamp=now.timestamp())
    finally:
        account.close()
    platform_registry.register(PlatformEntry(
        name=GATEWAY_PLATFORM_ID, label="隔离历史投影测试 Gateway",
        adapter_factory=OpenAndroidPlatformAdapter, check_fn=lambda: True,
    ))
    adapter = _adapter(core)

    async def deliver():
        progress = await adapter.send(conversation, "正在查询合成测试信息", metadata={"messageId": "msg_projection_progress"})
        assert progress.success
        database.append_message(
            "native_projection", "assistant", None,
            tool_calls=[{"id": "call_projection", "type": "function", "function": {"name": "web_search", "arguments": '{"query":"合成测试"}'}}],
            timestamp=now.timestamp() + 1,
        )
        database.append_message(
            "native_projection", "tool", '{"synthetic_result":"供模型使用的合成返回值"}',
            tool_name="web_search", tool_call_id="call_projection", timestamp=now.timestamp() + 2,
        )
        database.append_message("native_projection", "assistant", "这是合成测试结果", timestamp=now.timestamp() + 3)
        final = await adapter.send(conversation, "这是合成测试结果", metadata={"messageId": "msg_projection_final"})
        assert final.success

    try:
        asyncio.run(deliver())
        reopened = core.open_gateway_account(ACCOUNT_ID)
        try:
            rows = reopened.conversations.list_messages(conversation)["messages"]
            progress = next((row for row in rows if row["messageId"] == "msg_projection_progress"), None)
            assert progress is not None, "实时显示的工具进度必须在重新打开会话后保留原身份"
            assert progress["text"] == "正在查询合成测试信息"
            assert not any(row["sender"] == "assistant" and not row["text"] and row["parts"] == [{"type": "text", "text": ""}] for row in rows)
        finally:
            reopened.close()
    finally:
        database.close()
        platform_registry.unregister(GATEWAY_PLATFORM_ID)


def test_native_empty_tool_call_scaffolding_is_not_projected_as_a_plain_assistant_message(tmp_path):
    SessionDB = pytest.importorskip("hermes_state").SessionDB
    database = SessionDB(tmp_path / "native.db")
    core = create_gateway_core(tmp_path / "gateway", secret_store=make_secret_store())
    core.history_reader = HermesHistoryPort(core, SimpleNamespace(_db=database)).read
    account = core.open_gateway_account("acct_projection")
    try:
        conversation = account.conversations.create("cc_empty_tool", "工具对话", "cor_create")["conversationId"]
        database.create_session("native_projection", "open_android")
        account.agent_sessions.record(conversation, "inbound-message")
        assert account.agent_sessions.attach(conversation, "native_projection")
        database.append_message(
            "native_projection", "assistant", None,
            tool_calls=[{"id": "call_projection", "type": "function", "function": {"name": "web_search", "arguments": '{}'}}],
            timestamp=1,
        )
        rows = account.conversations.list_messages(conversation)["messages"]
        assert not any(
            row["sender"] == "assistant" and not row["text"] and row["parts"] == [{"type": "text", "text": ""}]
            for row in rows
        ), "原生工具调用必须保留结构或排除内部空记录，不能显示空回复气泡"
    finally:
        account.close()
        database.close()


def test_unmatched_native_legacy_user_text_is_still_visible(tmp_path):
    SessionDB = pytest.importorskip("hermes_state").SessionDB
    database = SessionDB(tmp_path / "native.db")
    core = create_gateway_core(tmp_path / "gateway", secret_store=make_secret_store())
    core.history_reader = HermesHistoryPort(core, SimpleNamespace(_db=database)).read
    account = core.open_gateway_account("acct_projection")
    try:
        conversation = account.conversations.create("cc_native_legacy", "旧宿主对话", "cor_create")["conversationId"]
        database.create_session("native_legacy", "open_android")
        account.agent_sessions.record(conversation, "inbound-message")
        assert account.agent_sessions.attach(conversation, "native_legacy")
        database.append_message("native_legacy", "user", "旧宿主保留的原始纯文本", timestamp=1)
        rows = account.conversations.list_messages(conversation)["messages"]
        assert len(rows) == 1
        assert rows[0]["text"] == "旧宿主保留的原始纯文本"
        assert rows[0]["parts"] == [{"type": "text", "text": "旧宿主保留的原始纯文本"}]
    finally:
        account.close()
        database.close()


def test_host_media_registration_keeps_large_originals_without_blocking_delivery(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    output = home / "outputs" / "large-original.bin"
    output.parent.mkdir(parents=True)
    with output.open("wb") as stream:
        for _ in range(26):
            stream.write(b"s" * (1024 * 1024))
    core = create_gateway_core(tmp_path / "gateway", secret_store=make_secret_store())
    account = core.open_gateway_account("acct_projection")
    try:
        conversation = account.conversations.create("cc_large", "大附件对话", "cor_create")["conversationId"]
        part = HistoryMedia(account).register(conversation, "msg_large", output)
        assert part["sizeBytes"] == 26 * 1024 * 1024
        assert part["mediaType"] == "application/octet-stream"
        assert output.is_file()
    finally:
        account.close()


def test_large_uploaded_attachment_reaches_native_agent_and_retains_host_original(tmp_path, monkeypatch):
    pytest.importorskip("hermes_state")
    from gateway.config import GatewayConfig, PlatformConfig
    from gateway.session import SessionStore
    from gateway.platform_registry import PlatformEntry, platform_registry
    from open_android_intelligence_gateway.adapter import OpenAndroidPlatformAdapter
    from open_android_intelligence_gateway.platform_identity import GATEWAY_PLATFORM_ID, GATEWAY_PLATFORM_IDS
    from test_assistant_message_identity import _adapter, ACCOUNT_ID

    home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_TEST_ISOLATION", str(home))
    core = create_gateway_core(tmp_path / "gateway", secret_store=make_secret_store())
    store = SessionStore(home / "sessions", GatewayConfig())
    port = HermesHistoryPort(core, store)
    core.history_reader = port.read
    for platform_id in GATEWAY_PLATFORM_IDS:
        platform_registry.register(PlatformEntry(name=platform_id, label="大附件隔离回归",
            adapter_factory=OpenAndroidPlatformAdapter, check_fn=lambda: True))
    adapter = _adapter(core)
    adapter.config = PlatformConfig(extra=adapter.config.extra)
    adapter.set_session_store(store)
    body = b"s" * (26 * 1024 * 1024)
    digest = hashlib.sha256(body).hexdigest()
    account = core.open_gateway_account(ACCOUNT_ID)
    try:
        conversation = account.conversations.create("cc_large_inbound", "大附件投递", "cor_create")["conversationId"]
        attachment = account.attachments.create(clientAttachmentId="ca_large", filename="large.bin",
            mediaType="application/octet-stream", sizeBytes=len(body), sha256=digest, correlationId="cor_upload")
        attachment_id = attachment["attachmentId"]
        account.attachments.upload_content(attachment_id, body)
        account.attachments.commit(attachment_id)
        accepted = account.conversations.accept_message(conversation, "cm_large_inbound", "读取合成大文件",
            [attachment_id], "dev_projection", "req_large", "cor_large")
    finally:
        account.close()

    captured = []
    async def agent(event):
        captured.extend(Path(path) for path in event.media_urls)
        assert len(captured) == 1
        assert captured[0].stat().st_size == len(body)
        with captured[0].open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == digest
        return "已收到合成大文件"

    async def deliver():
        adapter.set_message_handler(agent)
        await adapter._dispatch_agent_inbound(f"/open-android-intelligence/v2/conversations/{conversation}/messages",
            json.dumps({"clientMessageId":"cm_large_inbound", "text":"读取合成大文件", "attachments":[attachment_id]}).encode(),
            {}, ACCOUNT_ID)
        async with asyncio.timeout(10):
            while True:
                probe = core.open_gateway_account(ACCOUNT_ID)
                try:
                    if probe.conversations.dispatch_message("cm_large_inbound")["status"] == "completed":
                        break
                finally:
                    probe.close()
                await asyncio.sleep(0)
    try:
        asyncio.run(deliver())
        assert captured and not captured[0].exists(), "宿主完成后临时入站文件应已释放"
        reopened = core.open_gateway_account(ACCOUNT_ID)
        try:
            rows = reopened.conversations.list_messages(conversation)["messages"]
            user = next(row for row in rows if row["messageId"] == accepted["messageId"])
            assert user["text"] == "读取合成大文件"
            assert any(part.get("attachmentId") == attachment_id for part in user["parts"])
            media = HistoryMedia(reopened)
            record = media._get("history-media:" + attachment_id)
            original = Path(record["path"])
            assert original.is_file() and original.stat().st_size == len(body)
            with original.open("rb") as stream:
                assert hashlib.file_digest(stream, "sha256").hexdigest() == digest
            assert media.metadata(conversation, attachment_id)["status"] == "REMOTE_UNAVAILABLE"
        finally:
            reopened.close()
    finally:
        for database in store._db_handles.values():
            database.close()
        for platform_id in GATEWAY_PLATFORM_IDS:
            platform_registry.unregister(platform_id)


def test_presentation_updates_restart_without_changing_actual_native_model_replay(tmp_path):
    SessionDB = pytest.importorskip("hermes_state").SessionDB
    from gateway.run import _build_gateway_agent_history

    native_path = tmp_path / "native.db"
    database = SessionDB(native_path)
    core = create_gateway_core(tmp_path / "gateway", secret_store=make_secret_store())
    port = HermesHistoryPort(core, SimpleNamespace(_db=database))
    core.history_reader = port.read
    account = core.open_gateway_account("acct_projection")
    try:
        conversation = account.conversations.create("cc_replay", "展示重启回归", "cor_create")["conversationId"]
        database.create_session("native_replay", "open_android")
        account.agent_sessions.record(conversation, "inbound-message")
        assert account.agent_sessions.attach(conversation, "native_replay")
        database.append_message("native_replay", "user", "模型已加工的正文", api_content="模型专用 API 原字节", platform_message_id="cm_replay", timestamp=1)
        database.append_message("native_replay", "assistant", "合成最终回复", timestamp=2)
        before, _ = _build_gateway_agent_history(database.get_messages_as_conversation("native_replay"))
        payload = {"messageId":"msg_replay", "conversationId":conversation, "sender":"assistant",
            "parts":[{"type":"text", "text":"合成流式片段"}], "text":"合成流式片段", "timestamp":1000,
            "anchorClientMessageId":"cm_replay"}
        port.record_visible(account, payload, "STREAMING")
        port.record_visible(account, {**payload, "parts":[{"type":"text", "text":"合成最终回复"}],
            "text":"合成最终回复", "timestamp":9000})
        after, _ = _build_gateway_agent_history(database.get_messages_as_conversation("native_replay"))
        assert after == before
        assert not any(row["role"] == "session_meta" for row in database.get_messages("native_replay"))
        assert next(row for row in database.get_messages("native_replay") if row["role"] == "user")["api_content"] == "模型专用 API 原字节"
    finally:
        account.close()
        database.close()
    database = SessionDB(native_path)
    try:
        core.history_reader = HermesHistoryPort(core, SimpleNamespace(_db=database)).read
        reopened = core.open_gateway_account("acct_projection")
        try:
            rows = reopened.conversations.list_messages(conversation)["messages"]
            final = next(row for row in rows if row["messageId"] == "msg_replay")
            assert final["text"] == "合成最终回复" and final["state"] == "CONFIRMED"
            assert final["timestamp"] == 1000
            assert len([row for row in rows if row["sender"] == "assistant"]) == 1
            restored, _ = _build_gateway_agent_history(database.get_messages_as_conversation("native_replay"))
            assert restored == before
        finally:
            reopened.close()
    finally:
        database.close()


def test_native_compaction_retains_original_display_without_showing_model_summary(tmp_path):
    SessionDB = pytest.importorskip("hermes_state").SessionDB
    database = SessionDB(tmp_path / "native.db")
    core = create_gateway_core(tmp_path / "gateway", secret_store=make_secret_store())
    port = HermesHistoryPort(core, SimpleNamespace(_db=database))
    core.history_reader = port.read
    account = core.open_gateway_account("acct_projection")
    try:
        conversation = account.conversations.create("cc_compact", "压缩历史回归", "cor_create")["conversationId"]
        database.create_session("native_compact", "open_android")
        account.agent_sessions.record(conversation, "inbound-message")
        assert account.agent_sessions.attach(conversation, "native_compact")
        accepted = account.conversations.accept_message(conversation, "cm_compact", "原始纯文本提问", [],
            "dev_projection", "req_compact", "cor_compact")
        dispatch = account.conversations.claim_dispatch("cm_compact")
        port.capture_submission(account, dispatch)
        database.append_message("native_compact", "user", "原始纯文本提问", platform_message_id="cm_compact", timestamp=1)
        database.archive_and_compact("native_compact", [{"role":"user", "content":"仅供模型重放的压缩摘要",
            "_compressed_summary":True, "timestamp":2}])
        rows = account.conversations.list_messages(conversation)["messages"]
        user = next(row for row in rows if row["messageId"] == accepted["messageId"])
        assert user["text"] == "原始纯文本提问" and user["state"] != "DELETED"
        assert len(rows) == 1
    finally:
        account.close()
        database.close()


def test_captured_batch_members_keep_each_original_identity_and_text(tmp_path):
    SessionDB = pytest.importorskip("hermes_state").SessionDB
    database = SessionDB(tmp_path / "native.db")
    core = create_gateway_core(tmp_path / "gateway", secret_store=make_secret_store())
    port = HermesHistoryPort(core, SimpleNamespace(_db=database))
    core.history_reader = port.read
    account = core.open_gateway_account("acct_projection")
    try:
        conversation = account.conversations.create("cc_batch_history", "批次原稿回归", "cor_create")["conversationId"]
        database.create_session("native_batch", "open_android")
        account.agent_sessions.record(conversation, "inbound-message")
        assert account.agent_sessions.attach(conversation, "native_batch")
        body = {"clientBatchId":"cb_projection", "joinMode":"newline-v1", "members":[
            {"clientMessageId":"cm_batch_1", "text":"  第一条\n"},
            {"clientMessageId":"cm_batch_2", "text":"🙂 第二条  "}]}
        accepted = account.conversations.workflow.accept_batch(conversation, body,
            {"deviceId":"dev_projection", "requestId":"req_batch", "correlationId":"cor_batch"}, datetime.now(timezone.utc))
        dispatch = account.conversations.claim_dispatch("cm_batch_1")
        port.capture_submission(account, dispatch)
        database.append_message("native_batch", "user", "模型已加工的批次正文", platform_message_id="cm_batch_1", timestamp=1)
        account.conversations.update_dispatch_status(accepted["members"][0]["messageId"], "delivered", None,
            "cor_delivered", claim_token=dispatch["claimToken"])
        first = account.conversations.list_messages(conversation)["messages"]
        second = account.conversations.list_messages(conversation)["messages"]
        assert first == second
        assert {row["messageId"] for row in second} == {member["messageId"] for member in accepted["members"]}
        by_client = {row["clientMessageId"]:row for row in second}
        assert {key:row["text"] for key,row in by_client.items()} == {"cm_batch_1":"  第一条\n", "cm_batch_2":"🙂 第二条  "}
        assert all(row["batchId"] == accepted["batchId"] for row in second)
    finally:
        account.close()
        database.close()


def test_missing_old_gateway_original_uses_identity_without_guessing_model_text(tmp_path):
    SessionDB = pytest.importorskip("hermes_state").SessionDB
    database = SessionDB(tmp_path / "native.db")
    core = create_gateway_core(tmp_path / "gateway", secret_store=make_secret_store())
    core.history_reader = HermesHistoryPort(core, SimpleNamespace(_db=database)).read
    account = core.open_gateway_account("acct_projection")
    try:
        conversation = account.conversations.create("cc_lost_original", "旧原稿缺失", "cor_create")["conversationId"]
        database.create_session("native_lost_original", "open_android")
        account.agent_sessions.record(conversation, "inbound-message")
        assert account.agent_sessions.attach(conversation, "native_lost_original")
        accepted = account.conversations.accept_message(conversation, "cm_lost_original", "已经丢失的原稿", [],
            "dev_projection", "req_lost_original", "cor_lost_original")
        account.store.database.execute("UPDATE messages SET text='' WHERE message_id=?", (accepted["messageId"],))
        database.append_message("native_lost_original", "user", "[模型内部加工内容]\n无法从中可靠猜测原稿",
            platform_message_id="cm_lost_original", timestamp=1)
        rows = account.conversations.list_messages(conversation)["messages"]
        assert len(rows) == 1
        assert rows[0]["messageId"] == accepted["messageId"] and rows[0]["clientMessageId"] == "cm_lost_original"
        assert rows[0]["text"] == "" and rows[0]["parts"] == []
    finally:
        account.close()
        database.close()


def test_native_rewind_deletes_display_and_prevents_history_media_download(tmp_path, monkeypatch):
    SessionDB = pytest.importorskip("hermes_state").SessionDB
    home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    output = home / "outputs" / "original.txt"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"synthetic original")
    database = SessionDB(tmp_path / "native.db")
    core = create_gateway_core(tmp_path / "gateway", secret_store=make_secret_store())
    port = HermesHistoryPort(core, SimpleNamespace(_db=database))
    core.history_reader = port.read
    account = core.open_gateway_account("acct_projection")
    context = {"deviceId":"dev_projection", "pairingGeneration":1, "grantRevision":1}
    try:
        conversation = account.conversations.create("cc_rewind", "撤回历史回归", "cor_create")["conversationId"]
        database.create_session("native_rewind", "open_android")
        account.agent_sessions.record(conversation, "inbound-message")
        assert account.agent_sessions.attach(conversation, "native_rewind")
        # 展示信息不再是模型行，撤回回归必须包含实际被撤回的原生消息。
        database.append_message("native_rewind", "assistant", "原附件说明", platform_message_id="msg_rewind", timestamp=1)
        account.conversations.record_assistant_message(conversation, "msg_rewind", "原附件说明")
        part = HistoryMedia(account).register(conversation, "msg_rewind", output)
        port.record_visible(account, {"messageId":"msg_rewind", "conversationId":conversation,
            "sender":"assistant", "text":"原附件说明", "parts":[{"type":"text","text":"原附件说明"},part], "timestamp":1000})
        token = HistoryMedia(account).grant(conversation, part["attachmentId"], context)["grantId"]
        database.replace_messages("native_rewind", [], archive_dropped=True)
        rows = account.conversations.list_messages(conversation)["messages"]
        assert rows[0]["messageId"] == "msg_rewind" and rows[0]["state"] == "DELETED"
        assert rows[0]["text"] == "" and rows[0]["parts"] == []
        assert HistoryMedia(account).metadata(conversation, part["attachmentId"])["status"] == "REMOTE_UNAVAILABLE"
        from open_android_intelligence_gateway.core import GatewayError
        with pytest.raises(GatewayError, match="ATTACHMENT_NOT_FOUND"):
            HistoryMedia(account).content(conversation, part["attachmentId"], token, context)
    finally:
        account.close()
        database.close()


def test_account_delete_retires_only_owned_presentation_and_media(tmp_path, monkeypatch):
    SessionDB = pytest.importorskip("hermes_state").SessionDB
    from open_android_intelligence_gateway.host_history import PRESENTATION_KIND, PRESENTATION_KEY
    home = tmp_path / "hermes_home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    database = SessionDB(tmp_path / "native.db")
    core = create_gateway_core(tmp_path / "gateway", secret_store=make_secret_store())
    port = HermesHistoryPort(core, SimpleNamespace(_db=database))
    core.history_reader = port.read
    originals = {}
    model_before = {}
    for identity in ("acct_delete", "acct_keep"):
        account = core.open_gateway_account(identity)
        try:
            conversation = account.conversations.create("cc_"+identity, "账号隔离回归", "cor_create")["conversationId"]
            database.create_session(identity, "open_android")
            account.agent_sessions.record(conversation, "inbound-message")
            assert account.agent_sessions.attach(conversation, identity)
            database.append_message(identity, "user", "此账号的模型原始输入", timestamp=1)
            part_id = "att_"+identity
            source = home / "cache" / (identity+".bin")
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(identity.encode())
            parts = HistoryMedia(account).adopt_inbound(conversation, "msg_"+identity, [{
                "attachmentId":part_id, "filename":source.name, "mediaType":"application/octet-stream",
                "sizeBytes":source.stat().st_size, "sha256":hashlib.sha256(source.read_bytes()).hexdigest()}], [source])
            port.record_visible(account, {"messageId":"msg_"+identity, "conversationId":conversation,
                "sender":"assistant", "text":"该账号的附件说明", "parts":[{"type":"text","text":"该账号的附件说明"},*parts],
                "timestamp":1000})
            originals[identity] = Path(HistoryMedia(account)._get("history-media:"+part_id)["path"])
            model_before[identity] = [row for row in database.get_messages(identity) if row["role"] != "session_meta"]
        finally:
            account.close()
    foreign = database.append_message("acct_delete", "session_meta", "foreign_display_marker",
        display_kind=PRESENTATION_KIND, display_metadata={PRESENTATION_KEY:{"version":1, "accountId":"acct_delete",
            "storageOwner":"another_storage_owner"}}, timestamp=2)
    try:
        assert core.delete_gateway_account("acct_delete")
        assert not core.delete_gateway_account("acct_delete")
        rows = database.get_messages("acct_delete", include_inactive=True)
        assert [row for row in rows if row["role"] != "session_meta"] == model_before["acct_delete"]
        assert [row["id"] for row in rows if row["role"] == "session_meta"] == [foreign]
        assert [row for row in database.get_messages("acct_keep") if row["role"] != "session_meta"] == model_before["acct_keep"]
        assert any(json.loads(value).get("accountId") == "acct_keep"
                   for _, value in database.list_meta_prefix("oai_gateway_presentation_v2:"))
        assert not originals["acct_delete"].exists() and originals["acct_keep"].is_file()
    finally:
        database.close()


def test_equal_message_identity_in_copied_gateway_storage_cannot_overwrite_other_owner(tmp_path):
    SessionDB = pytest.importorskip("hermes_state").SessionDB
    database = SessionDB(tmp_path / "native.db")
    secret_store = make_secret_store()
    first = create_gateway_core(tmp_path / "gateway_first", secret_store=secret_store)
    first_port = HermesHistoryPort(first, SimpleNamespace(_db=database))
    first.history_reader = first_port.read
    account = first.open_gateway_account("acct_projection")
    try:
        conversation = account.conversations.create("cc_equal_identity", "储存归属隔离回归", "cor_create")["conversationId"]
        database.create_session("native_shared", "open_android")
        account.agent_sessions.record(conversation, "inbound-message")
        assert account.agent_sessions.attach(conversation, "native_shared")
        payload = {"messageId":"msg_equal_identity", "conversationId":conversation, "sender":"assistant",
            "text":"属于各自 Gateway 的通知", "parts":[{"type":"text","text":"属于各自 Gateway 的通知"}], "timestamp":1000}
        first_port.record_visible(account, payload)
    finally:
        account.close()
    # 只复制隔离测试的 Gateway 数据，模拟同一旧绑定来自两个不同储存归属。
    shutil.copytree(first.storage_root, tmp_path / "gateway_second")
    second = create_gateway_core(tmp_path / "gateway_second", secret_store=secret_store)
    second_port = HermesHistoryPort(second, SimpleNamespace(_db=database))
    second.history_reader = second_port.read
    account = second.open_gateway_account("acct_projection")
    try:
        second_port.record_visible(account, payload)
        before = account.conversations.list_messages(conversation)["messages"]
        assert len(before) == 1 and before[0]["text"] == payload["text"]
    finally:
        account.close()
    account = first.open_gateway_account("acct_projection")
    try:
        first_port.record_visible(account, {**payload, "text":"第一个 Gateway 后续更新",
            "parts":[{"type":"text","text":"第一个 Gateway 后续更新"}]})
    finally:
        account.close()
    try:
        account = second.open_gateway_account("acct_projection")
        try:
            assert account.conversations.list_messages(conversation)["messages"] == before
        finally:
            account.close()
    finally:
        database.close()
