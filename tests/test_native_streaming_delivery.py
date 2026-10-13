"""原生 Hermes 消费增量时，手机在生成完成前持续收到同一条回复。"""
from __future__ import annotations

import asyncio
import os

import pytest
from aiohttp import ClientSession
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from test_batch_http_dispatch import (
    NATIVE_HOST,
    PREFIX,
    _adapter,
    _next_event,
    _request,
    _signed_request,
)
from test_conversation_rename import ACCOUNT_ID, _gateway


async def _next_reply_event(stream):
    async with asyncio.timeout(2):
        while True:
            event = await _next_event(stream)
            if event["event"] in {"conversation.message.delta", "conversation.message.completed"}:
                return event


def test_native_consumer_delivers_three_progressive_updates_before_completion(tmp_path, monkeypatch):
    if not NATIVE_HOST:
        assert os.environ.get("OAI_REQUIRE_NATIVE_HERMES") != "1", "指定原生验证时不能以缺少宿主跳过"
        pytest.skip("本回归需加载原生 Hermes GatewayStreamConsumer")
    from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.platform_registry import PlatformEntry, platform_registry
    from open_android_intelligence_gateway.adapter import OpenAndroidPlatformAdapter
    from open_android_intelligence_gateway.platform_identity import GATEWAY_PLATFORM_IDS

    home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_TEST_ISOLATION", str(home))
    store = SessionStore(home / "sessions", GatewayConfig())
    for platform_id in GATEWAY_PLATFORM_IDS:
        platform_registry.register(PlatformEntry(
            name=platform_id, label="原生流式隔离回归",
            adapter_factory=OpenAndroidPlatformAdapter, check_fn=lambda: True,
        ))

    async def scenario():
        key = Ed25519PrivateKey.generate()
        core, bundle, conversation_id, exposure = _gateway(tmp_path, key)
        adapter = _adapter(core, exposure)
        adapter.set_session_store(store)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def agent(event):
            entered.set()
            await release.wait()

        adapter.set_message_handler(agent)
        assert await adapter.connect()
        base_url = f"http://127.0.0.1:{adapter._site._server.sockets[0].getsockname()[1]}"
        consumer = GatewayStreamConsumer(
            adapter=adapter,
            chat_id=conversation_id,
            config=StreamConsumerConfig(edit_interval=0.01, buffer_threshold=1, cursor=""),
            metadata={"account_id": ACCOUNT_ID},
        )
        task = None
        try:
            async with ClientSession() as client:
                _, headers = _signed_request(bundle, key, "GET", f"{PREFIX}/events", request_id="req_native_stream")
                headers["Accept"] = "text/event-stream"
                async with client.get(base_url + f"{PREFIX}/events", headers=headers) as stream:
                    assert stream.status == 200
                    # 经真实入口建立原生会话，增量回复必须拥有可重开的宿主历史。
                    await _request(client, base_url, bundle, key, "POST",
                        f"{PREFIX}/conversations/{conversation_id}/messages",
                        {"clientMessageId": "cm_native_stream", "text": "请给出三段合成回复", "attachments": []},
                        "req_native_stream_message")
                    await asyncio.wait_for(entered.wait(), 5)
                    task = asyncio.create_task(consumer.run())
                    events = []
                    for fragment, expected_text in (
                        ("第一段", "第一段"),
                        ("，第二段", "第一段，第二段"),
                        ("，第三段", "第一段，第二段，第三段"),
                    ):
                        consumer.on_delta(fragment)
                        event = await _next_reply_event(stream)
                        assert event["event"] == "conversation.message.delta"
                        assert event["payload"]["text"] == expected_text
                        assert event["payload"]["conversationId"] == conversation_id
                        events.append(event)
                    consumer.finish("第一段，第二段，第三段。")
                    completed = await _next_reply_event(stream)
                    assert completed["event"] == "conversation.message.completed"
                    assert completed["payload"]["text"] == "第一段，第二段，第三段。"
                    assert {event["payload"]["messageId"] for event in [*events, completed]} == {
                        events[0]["payload"]["messageId"],
                    }
                    await asyncio.wait_for(task, 2)
                    assert consumer.final_response_sent, "流式完成必须向原生宿主确认，避免再次发送最终回答"
                    assert consumer.delivered_final_matches("第一段，第二段，第三段。") is True
                    history = await _request(client, base_url, bundle, key, "GET",
                        f"{PREFIX}/conversations/{conversation_id}/messages", request_id="req_native_stream_history")
                    replies = [row for row in history["data"]["messages"] if row["sender"] == "assistant"]
                    assert len(replies) == 1
                    assert replies[0]["messageId"] == completed["payload"]["messageId"]
                    assert replies[0]["text"] == "第一段，第二段，第三段。"
                    assert replies[0]["timestamp"] == events[0]["payload"]["timestamp"]
        finally:
            release.set()
            consumer.finish()
            if task is not None:
                await asyncio.wait_for(task, 2)
            await adapter.disconnect()

    try:
        asyncio.run(scenario())
    finally:
        for database in store._db_handles.values():
            database.close()
        for platform_id in GATEWAY_PLATFORM_IDS:
            platform_registry.unregister(platform_id)
