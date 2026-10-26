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
async def running_turn(tmp_path, store, defer_completion=False, batched=False, on_interrupt=None):
    from gateway.run_adapters import GatewayAdapterLifecycleMixin
    key = Ed25519PrivateKey.generate()
    core, bundle, cid, exposure = _gateway(tmp_path, key)
    adapter = _adapter(core, exposure)
    adapter.set_session_store(store)
    entered, release = asyncio.Event(), asyncio.Event()
    started = []
    class Runner(GatewayAdapterLifecycleMixin):
        def _standalone_launch_scope(self):
            return nullcontext()
        async def handle(self, event):
            started.append(event)
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
            if on_interrupt is not None:
                await on_interrupt(adapter, release, *args, **kwargs)
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
            body = {"clientBatchId": "cb_review", "joinMode": "newline-v1", "members": [
                {"clientMessageId": "cm_review", "text": "Work"}, {"clientMessageId": "cm_review_second", "text": "More work"},
            ]} if batched else {"clientMessageId": "cm_review", "text": "Work", "attachments": []}
            resource = "message-batches" if batched else "messages"
            receipt = await _request(client, base, bundle, key, "POST", f"{PREFIX}/conversations/{cid}/{resource}",
                body, "req_review")
            await asyncio.wait_for(entered.wait(), 5)
            yield SimpleNamespace(core=core, adapter=adapter, cid=cid, gid=(receipt["data"] if batched else receipt["data"]["message"])["generationId"],
                client=client, base=base, bundle=bundle, key=key, callbacks=callbacks, complete=complete,
                runner=runner, release=release, started=started)
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


def test_cancel_capability_tracks_handler_removal_and_disconnect(native_store, tmp_path):
    async def scenario():
        async with running_turn(tmp_path, native_store) as turn:
            # _create_adapter installs this reference in both verified SDKs.
            turn.adapter.gateway_runner = turn.runner
            assert turn.core.generation_canceller is not None
            handler = turn.adapter._message_handler
            turn.adapter.set_message_handler(None)
            assert turn.core.generation_canceller is None
            turn.adapter.set_message_handler(handler)
            assert turn.core.generation_canceller is not None
            turn.release.set()
            async with asyncio.timeout(5):
                while turn.adapter._session_tasks:
                    await asyncio.sleep(0.01)
            await turn.adapter.disconnect()
            assert turn.core.generation_canceller is None
    asyncio.run(scenario())


def test_cancel_cannot_report_normal_completion_as_cancelled_or_cancel_successor(native_store, tmp_path):
    async def scenario():
        next_started, finish_next = asyncio.Event(), asyncio.Event()
        async def interrupt(adapter, release, session_key, source, **kwargs):
            original = adapter._session_tasks[session_key]
            release.set()
            # Real native task completion and callbacks race with the interrupt.
            await asyncio.shield(original)
            await asyncio.wait_for(next_started.wait(), 5)
        async with running_turn(tmp_path, native_store, on_interrupt=interrupt) as turn:
            original_handler = turn.adapter._message_handler
            async def handler(event):
                if event.message_id == "cm_successor":
                    next_started.set()
                    await finish_next.wait()
                    return "Successor completed"
                return await original_handler(event)
            # Keep the native runner reference when installing an instrumented
            # handler, as the real host's _create_adapter does.
            turn.adapter.gateway_runner = turn.runner
            turn.adapter.set_message_handler(handler)
            receipt = await _request(turn.client, turn.base, turn.bundle, turn.key, "POST",
                f"{PREFIX}/conversations/{turn.cid}/messages",
                {"clientMessageId": "cm_successor", "text": "Next", "attachments": []}, "req_successor")
            target = f"{PREFIX}/conversations/{turn.cid}/generations/{turn.gid}/cancel"
            try:
                result = await _request(turn.client, turn.base, turn.bundle, turn.key, "POST", target,
                    {"clientMessageId": "cm_cancel_race"}, "req_cancel_race")
                assert result["data"]["outcome"] == "ALREADY_COMPLETED"
                assert len(turn.adapter._session_tasks) == 1
                successor = next(iter(turn.adapter._session_tasks.values()))
                assert not successor.done() and not successor.cancelling()
                account = turn.core.open_gateway_account(ACCOUNT_ID)
                try:
                    assert account.conversations.workflow._get("generation:" + turn.gid)["state"] == "completed"
                    successor_gid = receipt["data"]["message"]["generationId"]
                    assert account.conversations.workflow._get("generation:" + successor_gid)["state"] == "running"
                finally:
                    account.close()
                assert not any(e["eventType"] == "conversation.generation.cancelled" for e in _events(turn.core))
            finally:
                finish_next.set()
    asyncio.run(scenario())


def test_unconfirmed_native_cancellation_keeps_generation_unknown_and_next_turn_queued(native_store, tmp_path):
    async def scenario():
        swallowed, finish = asyncio.Event(), asyncio.Event()
        async with running_turn(tmp_path, native_store) as turn:
            # The real native cancel API has a five-second unwind bound. A
            # callback which suppresses cancellation must never become a
            # successful cancellation receipt just because that API returned.
            original_handler = turn.adapter._message_handler
            async def handler(event):
                if event.message_id == "cm_stubborn":
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        swallowed.set()
                        await finish.wait()
                    return "Finished after cancellation timed out"
                return await original_handler(event)
            turn.release.set()
            async with asyncio.timeout(5):
                while turn.adapter._session_tasks:
                    await asyncio.sleep(0.01)
            turn.adapter.gateway_runner = turn.runner
            turn.adapter.set_message_handler(handler)
            receipt = await _request(turn.client, turn.base, turn.bundle, turn.key, "POST",
                f"{PREFIX}/conversations/{turn.cid}/messages",
                {"clientMessageId": "cm_stubborn", "text": "Wait", "attachments": []}, "req_stubborn")
            gid = receipt["data"]["message"]["generationId"]
            async with asyncio.timeout(5):
                while (ACCOUNT_ID, turn.cid, gid) not in turn.adapter._native_turns:
                    await asyncio.sleep(0.01)
            target = f"{PREFIX}/conversations/{turn.cid}/generations/{gid}/cancel"
            try:
                result = await _request(turn.client, turn.base, turn.bundle, turn.key, "POST", target,
                    {"clientMessageId": "cm_timeout_cancel"}, "req_timeout_cancel")
                assert swallowed.is_set()
                assert result["data"]["outcome"] == "OUTCOME_UNKNOWN"
                queued = await _request(turn.client, turn.base, turn.bundle, turn.key, "POST",
                    f"{PREFIX}/conversations/{turn.cid}/messages",
                    {"clientMessageId": "cm_after_timeout", "text": "Next", "attachments": []}, "req_after_timeout")
                await asyncio.sleep(0.25)
                account = turn.core.open_gateway_account(ACCOUNT_ID)
                try:
                    assert account.conversations.workflow._get("generation:" + gid)["state"] == "unknown"
                    next_gid = queued["data"]["message"]["generationId"]
                    assert account.conversations.workflow._get("generation:" + next_gid)["state"] == "queued"
                finally:
                    account.close()
                assert not any(e["eventType"] == "conversation.generation.cancelled" for e in _events(turn.core))
            finally:
                finish.set()
                async with asyncio.timeout(5):
                    while (ACCOUNT_ID, turn.cid, gid) in turn.adapter._native_turns:
                        await asyncio.sleep(0.01)
    asyncio.run(scenario())


@pytest.mark.parametrize("batched", [False, True], ids=["single", "batch"])
@pytest.mark.parametrize("defer_completion", [False, True], ids=["callback-before-cancel", "callback-after-cancel"])
def test_cancel_closes_only_published_content_in_either_callback_order(native_store, tmp_path, monkeypatch, defer_completion, batched):
    async def scenario():
        async with running_turn(tmp_path, native_store, defer_completion, batched) as turn:
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
            failed = [e for e in statuses if e["payload"]["status"] == "failed"]
            assert len(failed) == (2 if batched else 1)
            assert {e["payload"]["clientMessageId"] for e in failed} == ({"cm_review", "cm_review_second"} if batched else {"cm_review"})
            assert all(e["payload"]["generationId"] == turn.gid and e["payload"]["errorCode"] == "AGENT_UNAVAILABLE" for e in failed)
            assert all(events.index(finals[0]) < events.index(e) for e in failed)
            before_replay = len(events)
            assert await _request(turn.client, turn.base, turn.bundle, turn.key, "POST", target,
                {"clientMessageId": "cm_cancel_review"}, "req_cancel_review") == receipt
            assert len(_events(turn.core)) == before_replay
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
