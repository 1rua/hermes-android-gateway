"""独立审查：真实多配置 SessionStore 是否能清理压缩祖先的展示元数据。"""
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import sys
import tempfile

case = Path(tempfile.mkdtemp(prefix="oai-history-standards-profile-"))
home = case / "hermes-home"
os.environ["HERMES_HOME"] = str(home)
os.environ["HERMES_TEST_ISOLATION"] = str(home)
plugin = Path(os.environ.get("OAI_REVIEW_PLUGIN_ROOT", str(Path(__file__).resolve().parents[4])))
sys.path.insert(0, str(plugin))
sys.path.insert(0, str(plugin / "tests"))
from gateway.config import GatewayConfig
from gateway.session import SessionEntry, SessionStore
from open_android_intelligence_gateway.core import create_gateway_core
from open_android_intelligence_gateway.host_history import HermesHistoryPort
from test_support import make_secret_store

store = SessionStore(home / "sessions", GatewayConfig(multiplex_profiles=True))
profile_home = home / "profiles" / "standards"
profile_home.mkdir(parents=True, mode=0o700)
store._profile_home_cache["standards"] = profile_home
key = "agent:standards:open_android:dm:standards-chat"
now = datetime.now(timezone.utc)
store._entries[key] = SessionEntry(session_key=key, session_id="native_profile_parent", created_at=now, updated_at=now)
database = store._db_for_session_id("native_profile_parent")
assert database is not store._db_for_session_id("unknown_session")
core = create_gateway_core(case / "gateway", secret_store=make_secret_store())
port = HermesHistoryPort(core, store)
core.history_reader = port.read
account = core.open_gateway_account("acct_standards_profile")
try:
    conversation = account.conversations.create("cc_standards_profile", "合成多配置清理", "cor_create")["conversationId"]
    database.create_session("native_profile_parent", "open_android", session_key=key)
    account.agent_sessions.record(conversation, "inbound-message")
    assert account.agent_sessions.attach(conversation, "native_profile_parent", key)
    port.record_visible(account, {"messageId":"msg_standards_parent", "conversationId":conversation,
        "sender":"assistant", "text":"合成祖先回复", "parts":[{"type":"text", "text":"合成祖先回复"}], "timestamp":1000})
    database.end_session("native_profile_parent", "compression")
    database.create_session("native_profile_child", "open_android", parent_session_id="native_profile_parent", session_key=key)
    # 压缩只推进宿主当前路由，Gateway 的稳定会话绑定保持原会话身份。
    store._publish_transcript_reroute("native_profile_parent", "native_profile_child")
finally:
    account.close()
try:
    before = {sid:len([row for row in database.get_messages(sid, include_inactive=True) if row["role"] == "session_meta"])
        for sid in ("native_profile_parent", "native_profile_child")}
    port.cleanup_account("acct_standards_profile")
    after = {sid:len([row for row in database.get_messages(sid, include_inactive=True) if row["role"] == "session_meta"])
        for sid in before}
    print(json.dumps({"pluginCommit":"a4a6acc855e2499ef11857a0f9ca438e7f424f63",
        "storeModule":sys.modules[SessionStore.__module__].__file__,
        "parentResolvesToOwningDb":store._db_for_session_id("native_profile_parent") is database,
        "childResolvesToOwningDb":store._db_for_session_id("native_profile_child") is database,
        "before":before, "after":after}, ensure_ascii=False, indent=2))
    assert sum(after.values()) == 0, "清理成功返回后仍在所属配置数据库保留当前账号的祖先展示正文"
finally:
    store.close_all_db_handles()
