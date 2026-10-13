"""隔离复现宿主显式编辑后同一客户端身份的显示记录。"""
from pathlib import Path
from types import SimpleNamespace
import json
import os
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from hermes_state import SessionDB
from open_android_intelligence_gateway.core import create_gateway_core
from open_android_intelligence_gateway.host_history import HermesHistoryPort
from test_support import make_secret_store

run = Path(tempfile.mkdtemp(prefix="oai-history-spec-edit-"))
os.environ["HERMES_HOME"] = str(run / "home")
os.environ["HERMES_TEST_ISOLATION"] = str(run / "home")
database = SessionDB(run / "native.db")
core = create_gateway_core(run / "gateway", secret_store=make_secret_store())
port = HermesHistoryPort(core, SimpleNamespace(_db=database))
core.history_reader = port.read
account = core.open_gateway_account("acct_review")
try:
    conversation = account.conversations.create("cc_review", "隔离编辑验证", "cor_review")["conversationId"]
    database.create_session("native_review", "open_android")
    account.agent_sessions.record(conversation, "inbound-message")
    assert account.agent_sessions.attach(conversation, "native_review")
    accepted = account.conversations.accept_message(conversation, "cm_review", "原始合成提问", [],
        "dev_review", "req_review", "cor_review")
    dispatch = account.conversations.claim_dispatch("cm_review")
    port.capture_submission(account, dispatch)
    database.append_message("native_review", "user", "模型已加工的原始合成提问",
        platform_message_id="cm_review", timestamp=1)
    account.conversations.update_dispatch_status(accepted["messageId"], "delivered", None,
        "cor_review", claim_token=dispatch["claimToken"])
    before = account.conversations.list_messages(conversation)["messages"]
    kept = database.get_messages_as_conversation("native_review")
    for row in kept:
        if row["role"] == "user":
            row["content"] = "用户在宿主明确编辑后的新合成提问"
    database.replace_messages("native_review", kept, archive_dropped=True)
    after = account.conversations.list_messages(conversation)["messages"]
    native = database.get_messages("native_review", include_inactive=True)
    print(json.dumps({"sourceRevision": "a4a6acc855e2499ef11857a0f9ca438e7f424f63",
        "nativeSdkRevision": "75b083e9399115b12513385252811f09c3503c90",
        "nativeExplicitEditArchivedOldUser": any(row["role"] == "user" and not row["active"] and not row["compacted"] for row in native),
        "nativeActiveUserIsEdited": any(row["role"] == "user" and row["active"] and row["content"] == "用户在宿主明确编辑后的新合成提问" for row in native),
        "beforeWire": [{"messageId": row["messageId"], "text": row["text"], "state": row["state"]} for row in before],
        "afterWire": [{"messageId": row["messageId"], "text": row["text"], "state": row["state"]} for row in after]},
        ensure_ascii=False, indent=2))
finally:
    account.close()
    database.close()
