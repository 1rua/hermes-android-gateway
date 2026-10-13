"""隔离复现同一句旧回答与新发布回答的身份关联。"""
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

run = Path(tempfile.mkdtemp(prefix="oai-history-spec-equal-reply-"))
os.environ["HERMES_HOME"] = str(run / "home")
os.environ["HERMES_TEST_ISOLATION"] = str(run / "home")
database = SessionDB(run / "native.db")
core = create_gateway_core(run / "gateway", secret_store=make_secret_store())
port = HermesHistoryPort(core, SimpleNamespace(_db=database))
core.history_reader = port.read
account = core.open_gateway_account("acct_review")
try:
    conversation = account.conversations.create("cc_review", "隔离身份验证", "cor_review")["conversationId"]
    database.create_session("native_review", "open_android")
    account.agent_sessions.record(conversation, "inbound-message")
    assert account.agent_sessions.attach(conversation, "native_review")
    old_native_id = database.append_message("native_review", "assistant", "相同的合成回答", timestamp=1)
    port.record_visible(account, {"messageId": "msg_new_notice", "conversationId": conversation,
        "sender": "assistant", "text": "相同的合成回答", "parts": [{"type": "text", "text": "相同的合成回答"}], "timestamp": 2000}, "STREAMING")
    mapping = account.store.database.execute("SELECT value FROM account_metadata WHERE key=?",
        (port._native_key("native_review", {"id": old_native_id}),)).fetchone()[0]
    rows_before_new_native = account.conversations.list_messages(conversation)["messages"]
    database.append_message("native_review", "assistant", "相同的合成回答", timestamp=2)
    rows_after_new_native = account.conversations.list_messages(conversation)["messages"]
    print(json.dumps({"sourceRevision": "a4a6acc855e2499ef11857a0f9ca438e7f424f63",
        "nativeSdkRevision": "75b083e9399115b12513385252811f09c3503c90", "oldNativeRowMappedAsNewMessage": mapping == "msg_new_notice",
        "beforeNewNativeReply": [{"messageId": row["messageId"], "timestamp": row["timestamp"]} for row in rows_before_new_native],
        "afterNewNativeReply": [{"messageId": row["messageId"], "timestamp": row["timestamp"]} for row in rows_after_new_native]},
        ensure_ascii=False, indent=2))
finally:
    account.close()
    database.close()
