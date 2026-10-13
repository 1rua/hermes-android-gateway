"""独立审查：用真实宿主压缩 API 检查携带尾部是否被误判为删除。"""
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import os
import sys
import tempfile

case = Path(tempfile.mkdtemp(prefix="oai-history-standards-compaction-"))
os.environ["HERMES_HOME"] = str(case / "hermes-home")
os.environ["HERMES_TEST_ISOLATION"] = os.environ["HERMES_HOME"]
plugin = Path(os.environ.get("OAI_REVIEW_PLUGIN_ROOT", str(Path(__file__).resolve().parents[4])))
sys.path.insert(0, str(plugin))
sys.path.insert(0, str(plugin / "tests"))
from hermes_state import SessionDB
from open_android_intelligence_gateway.core import create_gateway_core
from open_android_intelligence_gateway.host_history import HermesHistoryPort
from test_support import make_secret_store

database = SessionDB(case / "native.db")
core = create_gateway_core(case / "gateway", secret_store=make_secret_store())
port = HermesHistoryPort(core, SimpleNamespace(_db=database))
core.history_reader = port.read
account = core.open_gateway_account("acct_standards_probe")
session_id = "native_standards_probe"
try:
    conversation = account.conversations.create("cc_standards_probe", "合成压缩审查", "cor_create")["conversationId"]
    database.create_session(session_id, "open_android")
    account.agent_sessions.record(conversation, "inbound-message")
    assert account.agent_sessions.attach(conversation, session_id)
    accepted = account.conversations.accept_message(conversation, "cm_standards_probe", "合成原始提问", [],
        "dev_standards", "req_standards", "cor_standards")
    dispatch = account.conversations.claim_dispatch("cm_standards_probe")
    port.capture_submission(account, dispatch)
    database.append_message(session_id, "user", "合成模型输入", platform_message_id="cm_standards_probe", timestamp=1)
    database.append_message(session_id, "assistant", "合成最终回复", timestamp=2)
    account.conversations.record_assistant_message(conversation, "msg_standards_final", "合成最终回复")
    port.record_visible(account, {"messageId":"msg_standards_final", "conversationId":conversation,
        "sender":"assistant", "text":"合成最终回复", "parts":[{"type":"text", "text":"合成最终回复"}], "timestamp":2000})
    account.conversations.update_dispatch_status(accepted["messageId"], "delivered", None, "cor_standards",
        claim_token=dispatch["claimToken"])
    account.conversations.update_dispatch_status(accepted["messageId"], "completed", None, "cor_standards")
    before = account.conversations.list_messages(conversation)["messages"]
    tail = [{key:row[key] for key in ("role", "content", "timestamp", "platform_message_id") if row.get(key) is not None}
        for row in database.get_messages(session_id) if row["role"] in {"user", "assistant"}]
    database.archive_and_compact(session_id, [{"role":"user", "content":"合成模型摘要",
        "_compressed_summary":True, "timestamp":0}, *tail], tail_count=len(tail))
    after = account.conversations.list_messages(conversation)["messages"]
    print(json.dumps({"pluginCommit":"a4a6acc855e2499ef11857a0f9ca438e7f424f63",
        "hostModule":sys.modules[SessionDB.__module__].__file__, "before":before, "after":after,
        "nativeFlags":[{key:row.get(key) for key in ("id","role","active","compacted","platform_message_id")}
            for row in database.get_messages(session_id, include_inactive=True)]}, ensure_ascii=False, indent=2))
    assert next(row for row in after if row["messageId"] == "msg_standards_final")["state"] != "DELETED", \
        "宿主压缩保留了回复，展示层却把它标记为删除"
finally:
    account.close()
    database.close()
