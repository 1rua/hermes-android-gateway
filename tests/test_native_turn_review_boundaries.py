"""Real SDK regressions for asynchronous deliveries and cancellation ordering."""
import asyncio
from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from test_native_turn_lifecycle import native_store, _events
from test_batch_http_dispatch import PREFIX, _adapter, _request
from test_conversation_rename import ACCOUNT_ID, _gateway


@asynccontextmanager
async def running_turn(tmp_path, store, defer_completion=False):
    from gateway.run_adapters import GatewayAdapterLifecycleMixin
    key = Ed25519PrivateKey.generate()
    core, bundle, cid, exposure = _gateway(tmp_path, key)
    adapter = _adapter(core, exposure)
    adapter.set_session_store(store)
    entered, release = asyncio.Event(), asyncio.Event()
    class Runner(GatewayAdapterLifecycleMixin):
        def _standalone_launch_scope(self):
            return nullcontext()
        async def handle(self, event):
            account = core.open_gateway_account(ACCOUNT_ID)
            try:
                sid = account.agent_sessions.lookup(cid)["agentSessionId"]
            finally:
                account.close()
            store.append_to_transcript(sid, {"role": "user", "content": event.text, "message_id": event.message_id})
            await adapter.send(cid, "Known partial", metadata={"expect_edits": True, "messageId": "msg_known"})
            entered.set()
            await release.wait()
        async def _interrupt_and_clear_session(self, *args, **kwargs):
            pass
    runner = Runner()
    adapter.set_message_handler(runner._standalone_scoped(runner.handle))
    callbacks = []
    complete = adapter.on_processing_complete
    if defer_completion:
        async def park(event, outcome):
            callbacks.append((event, outcome))
        adapter.on_processing_complete = park
    assert await adapter.connect()
    base = f"http://127.0.0.1:{adapter._site._server.sockets[0].getsockname()[1]}"
    try:
        async with ClientSession() as client:
            receipt = await _request(client, base, bundle, key, "POST", f"{PREFIX}/conversations/{cid}/messages",
                {"clientMessageId": "cm_review", "text": "Work", "attachments": []}, "req_review")
            await asyncio.wait_for(entered.wait(), 5)
            yield SimpleNamespace(core=core, adapter=adapter, cid=cid, gid=receipt["data"]["message"]["generationId"],
                client=client, base=base, bundle=bundle, key=key, callbacks=callbacks, complete=complete)
    finally:
        release.set()
        async with asyncio.timeout(5):
            while adapter._session_tasks:
                await asyncio.sleep(0.01)
        for event, outcome in callbacks:
            await complete(event, outcome)
        await adapter.disconnect()


def test_unrelated_async_deliveries_do_not_inherit_running_generation(native_store, tmp_path):
    async def scenario():
        async with running_turn(tmp_path, native_store) as turn:
            from open_android_intelligence_gateway.device_tools import trusted_turn
            assert trusted_turn.get() is None
            history = turn.core.history_reader
            # First exercise a real historical user origin, then the old
            # workflow.current fallback with no native history port available.
            for mid, reader in (("msg_reminder_history", history), ("msg_reminder_no_history", None)):
                turn.core.history_reader = reader
                await asyncio.create_task(turn.adapter.send(turn.cid, mid,
                    metadata={"expect_edits": True, "messageId": mid}))
                event = _events(turn.core)[-1]
                assert event["payload"]["messageId"] == mid
                assert "generationId" not in event["payload"]
            turn.core.history_reader = history
            pending = turn.adapter._streaming_replies[(ACCOUNT_ID, turn.cid, turn.gid)]
            assert set(pending) == {"msg_known"}
            known = next(e for e in _events(turn.core) if e["payload"].get("messageId") == "msg_known")
            assert known["payload"]["generationId"] == turn.gid
        completed = [e["payload"]["messageId"] for e in _events(turn.core) if e["eventType"] == "conversation.message.completed"]
        assert "msg_known" in completed
        assert not any(mid.startswith("msg_reminder_") for mid in completed)
    asyncio.run(scenario())


@pytest.mark.parametrize("defer_completion", [False, True], ids=["callback-before-cancel", "callback-after-cancel"])
def test_cancel_closes_only_published_content_in_either_callback_order(native_store, tmp_path, monkeypatch, defer_completion):
    async def scenario():
        async with running_turn(tmp_path, native_store, defer_completion) as turn:
            from open_android_intelligence_gateway.history_media import HistoryMedia
            # A later media lookup must not add content to a cancelled message's
            # finalization. Only the already published snapshot may be closed.
            monkeypatch.setattr(HistoryMedia, "reply_parts", lambda *args: [{"type": "text", "text": "LATE MEDIA"}])
            target = f"{PREFIX}/conversations/{turn.cid}/generations/{turn.gid}/cancel"
            receipt = await _request(turn.client, turn.base, turn.bundle, turn.key, "POST", target,
                {"clientMessageId": "cm_cancel_review"}, "req_cancel_review")
            assert receipt["data"]["outcome"] == "CANCELLED"
            if defer_completion:
                assert len(turn.callbacks) == 1
                event, outcome = turn.callbacks[0]
                await turn.complete(event, outcome)
            events = _events(turn.core)
            finals = [e for e in events if e["eventType"] == "conversation.message.completed" and e["payload"].get("messageId") == "msg_known"]
            assert len(finals) == 1
            assert finals[0]["payload"]["text"] == "Known partial"
            assert finals[0]["payload"]["parts"] == [{"type": "text", "text": "Known partial"}]
            assert finals[0]["payload"]["generationId"] == turn.gid
            statuses = [e for e in events if e["eventType"] == "conversation.message.status"]
            assert statuses[-1]["payload"]["status"] == "failed"
            assert statuses[-1]["payload"]["generationId"] == turn.gid
            assert len([e for e in statuses if e["payload"]["status"] == "failed"]) == 1
            assert turn.adapter._streaming_replies == {}
            account = turn.core.open_gateway_account(ACCOUNT_ID)
            try:
                assert account.conversations.workflow.for_message(statuses[-1]["payload"]["messageId"])["state"] == "cancelled"
                assert account.conversations.dispatch_message("cm_review")["status"] == "failed"
                history = account.conversations.list_messages(turn.cid)["messages"]
                assert next(m for m in history if m["messageId"] == "msg_known")["state"] == "CONFIRMED"
            finally:
                account.close()
            before = len(events)
            await turn.adapter._publish_event("conversation.message.completed", turn.cid, "msg_new_late", "New late output",
                account_id=ACCOUNT_ID, generation_id=turn.gid)
            await turn.adapter._publish_event("conversation.message.completed", turn.cid, "msg_known", "Changed content",
                account_id=ACCOUNT_ID, generation_id=turn.gid)
            assert len(_events(turn.core)) == before
            if defer_completion:
                await turn.complete(event, outcome)
                assert len(_events(turn.core)) == before
    asyncio.run(scenario())
