"""隔离复现宿主撤回后首次时间线读取之前的历史媒体授权。"""
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
from open_android_intelligence_gateway.core import create_gateway_core, GatewayError
from open_android_intelligence_gateway.history_media import HistoryMedia
from open_android_intelligence_gateway.host_history import HermesHistoryPort
from test_support import make_secret_store

run = Path(tempfile.mkdtemp(prefix="oai-history-spec-deletion-"))
home = run / "hermes_home"
os.environ["HERMES_HOME"] = str(home)
os.environ["HERMES_TEST_ISOLATION"] = str(home)
output = home / "outputs" / "synthetic.txt"
output.parent.mkdir(parents=True)
output.write_bytes(b"synthetic original")
database = SessionDB(run / "native.db")
core = create_gateway_core(run / "gateway", secret_store=make_secret_store())
port = HermesHistoryPort(core, SimpleNamespace(_db=database))
core.history_reader = port.read
account = core.open_gateway_account("acct_review")
context = {"deviceId": "dev_review", "pairingGeneration": 1, "grantRevision": 1}
try:
    conversation = account.conversations.create("cc_review", "隔离撤回验证", "cor_review")["conversationId"]
    database.create_session("native_review", "open_android")
    account.agent_sessions.record(conversation, "inbound-message")
    assert account.agent_sessions.attach(conversation, "native_review")
    media = HistoryMedia(account)
    part = media.register(conversation, "msg_review", output)
    port.record_visible(account, {"messageId": "msg_review", "conversationId": conversation,
        "sender": "assistant", "text": "合成附件说明", "parts": [{"type": "text", "text": "合成附件说明"}, part], "timestamp": 1000})
    first = media.grant(conversation, part["attachmentId"], context)["grantId"]
    second = media.grant(conversation, part["attachmentId"], context)["grantId"]
    database.replace_messages("native_review", [], archive_dropped=True)
    before_metadata = media.metadata(conversation, part["attachmentId"])
    before_content, _ = media.content(conversation, part["attachmentId"], first, context)
    fresh_grant = media.grant(conversation, part["attachmentId"], context)
    rows = account.conversations.list_messages(conversation)["messages"]
    after_metadata = media.metadata(conversation, part["attachmentId"])
    try:
        media.content(conversation, part["attachmentId"], second, context)
        after_code = "ALLOWED"
    except GatewayError as error:
        after_code = error.code
    print(json.dumps({"sourceRevision": "a4a6acc855e2499ef11857a0f9ca438e7f424f63",
        "nativeSdkRevision": "75b083e9399115b12513385252811f09c3503c90",
        "afterNativeRewindBeforeTimelineRead": {"remoteAvailable": before_metadata["remoteAvailable"],
            "existingGrantReturnsOriginal": before_content == b"synthetic original", "newGrantAllowed": bool(fresh_grant)},
        "afterTimelineRead": {"messageState": rows[0]["state"], "remoteAvailable": after_metadata["remoteAvailable"],
            "contentError": after_code}}, ensure_ascii=False, indent=2))
finally:
    account.close()
    database.close()
