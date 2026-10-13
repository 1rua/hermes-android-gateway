"""原生历史 API 缺失时公开发送接口不能声称回复已经送达。"""
from pathlib import Path
from types import SimpleNamespace
import asyncio
import tempfile
import sys

root = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "tests"))
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from gateway.platform_registry import platform_registry
from test_batch_http_dispatch import NATIVE_HOST, _adapter
from test_conversation_rename import ACCOUNT_ID, _gateway


async def probe(store_after_connect):
    case_root = Path(tempfile.mkdtemp(prefix="oai-history-final-20261013-missing-api-"))
    core, _, conversation_id, exposure = _gateway(case_root, Ed25519PrivateKey.generate())
    adapter = _adapter(core, exposure)
    unavailable_store = SimpleNamespace(_db=None)
    account = core.open_gateway_account(ACCOUNT_ID)
    try:
        account.agent_sessions.record(conversation_id, "inbound-message")
        assert account.agent_sessions.attach(conversation_id, "native-api-unavailable")
    finally:
        account.close()
    if not store_after_connect:
        adapter.set_session_store(unavailable_store)
    try:
        assert await adapter.connect()
        if store_after_connect:
            adapter.set_session_store(unavailable_store)
        assert core.history_reader is not None
        sent = await adapter.send(conversation_id, "合成未送达回复", metadata={"account_id": ACCOUNT_ID})
        assert sent.success is False
        assert sent.error == "HOST_HISTORY_UNAVAILABLE"
        account = core.open_gateway_account(ACCOUNT_ID)
        try:
            assert not any(event["eventType"] == "conversation.message.completed" for event in account.events.read_after(None))
        finally:
            account.close()
        print("连接后注入" if store_after_connect else "连接前注入", "HOST_HISTORY_UNAVAILABLE；没有完成事件")
    finally:
        await adapter.disconnect()
        platform_registry.unregister(adapter.platform.value)


assert NATIVE_HOST, "此探针必须调用真实 Hermes BasePlatformAdapter"
asyncio.run(probe(False))
asyncio.run(probe(True))
