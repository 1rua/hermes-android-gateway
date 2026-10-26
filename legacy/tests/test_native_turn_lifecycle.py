"""Exercise real Hermes session storage, scoped callbacks and processing tasks.

No model/provider is needed: the native adapter owns task admission, sends the
final reply, invokes lifecycle hooks and cancels its actual background task.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import nullcontext
import hashlib
import os
from pathlib import Path

import pytest
from aiohttp import ClientSession
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from test_batch_http_dispatch import NATIVE_HOST, PREFIX, _adapter, _request, _signed_request, _b64url
from test_conversation_rename import ACCOUNT_ID, _gateway
from test_conversation_new_command import _negotiation_body


@pytest.fixture
def native_store(tmp_path, monkeypatch):
    if not NATIVE_HOST:
        assert os.environ.get("OAI_REQUIRE_NATIVE_HERMES") != "1", "Native Hermes runtime is required"
        pytest.skip("Requires the real Hermes SDK")
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.platform_registry import PlatformEntry, platform_registry
    from open_android_intelligence_gateway.adapter import OpenAndroidPlatformAdapter
    from open_android_intelligence_gateway.platform_identity import GATEWAY_PLATFORM_IDS
    home = tmp_path / "native-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_TEST_ISOLATION", str(home))
    for platform_id in GATEWAY_PLATFORM_IDS:
        platform_registry.register(PlatformEntry(name=platform_id, label="Native turn test",
            adapter_factory=OpenAndroidPlatformAdapter, check_fn=lambda: True))
    store = SessionStore(home / "sessions", GatewayConfig())
    yield store
    for database in store._db_handles.values():
        database.close()
    for platform_id in GATEWAY_PLATFORM_IDS:
        platform_registry.unregister(platform_id)


def _events(core):
    account = core.open_gateway_account(ACCOUNT_ID)
    try:
        return account.events.read_after(None)
    finally:
        account.close()


def test_signed_cancel_reaches_native_task_through_real_scoped_handler(native_store, tmp_path):
    """The catalog's old 9d642b4 release returned UNSUPPORTED for this callback."""
    from gateway.run_adapters import GatewayAdapterLifecycleMixin
    async def scenario():
        key = Ed25519PrivateKey.generate()
        core, bundle, cid, exposure = _gateway(tmp_path, key)
        adapter = _adapter(core, exposure)
        adapter.set_session_store(native_store)
        entered, release = asyncio.Event(), asyncio.Event()
        interrupts = []
        class Runner(GatewayAdapterLifecycleMixin):
            def _standalone_launch_scope(self):
                return nullcontext()
            async def handle(self, event):
                entered.set()
                await release.wait()
            async def _interrupt_and_clear_session(self, session_key, source, **kwargs):
                interrupts.append((session_key, source.chat_id))
        runner = Runner()
        adapter.set_message_handler(runner._standalone_scoped(runner.handle))
        assert await adapter.connect()
        base = f"http://127.0.0.1:{adapter._site._server.sockets[0].getsockname()[1]}"
        try:
            async with ClientSession() as client:
                receipt = await _request(client, base, bundle, key, "POST", f"{PREFIX}/conversations/{cid}/messages",
                    {"clientMessageId": "cm_stop_wrapper", "text": "Work", "attachments": []}, "req_stop_work")
                gid = receipt["data"]["message"]["generationId"]
                await asyncio.wait_for(entered.wait(), 5)
                result = await _request(client, base, bundle, key, "POST",
                    f"{PREFIX}/conversations/{cid}/generations/{gid}/cancel",
                    {"requestId": "req_stop_wrapper"}, "req_stop_wrapper")
                assert result["data"]["outcome"] == "CANCELLED"
                assert len(interrupts) == 1 and interrupts[0][1] == cid
                assert not adapter._session_tasks
        finally:
            release.set()
            async with asyncio.timeout(5):
                while adapter._session_tasks:
                    await asyncio.sleep(0.01)
            await adapter.disconnect()
    asyncio.run(scenario())


def test_post_creates_distinct_empty_host_sessions_without_new_and_retry_reuses(native_store, tmp_path):
    async def scenario():
        key = Ed25519PrivateKey.generate()
        core, bundle, _, exposure = _gateway(tmp_path, key)
        adapter = _adapter(core, exposure)
        adapter.set_session_store(native_store)
        seen = []
        async def handler(event):
            seen.append(event.text)
        adapter.set_message_handler(handler)
        assert await adapter.connect()
        base = f"http://127.0.0.1:{adapter._site._server.sockets[0].getsockname()[1]}"
        try:
            async with ClientSession() as client:
                body = {"clientConversationId": "cc_direct", "title": "New conversation"}
                first = await _request(client, base, bundle, key, "POST", PREFIX + "/conversations", body, "req_direct")
                retry = await _request(client, base, bundle, key, "POST", PREFIX + "/conversations", body, "req_direct_retry")
                second = await _request(client, base, bundle, key, "POST", PREFIX + "/conversations",
                    {**body, "clientConversationId": "cc_another"}, "req_another")
            assert retry["data"] == first["data"]
            ids = [r["data"]["conversation"]["conversationId"] for r in (first, second)]
            assert ids[0] != ids[1]
            account = core.open_gateway_account(ACCOUNT_ID)
            try:
                bindings = [account.agent_sessions.lookup(cid) for cid in ids]
                assert all(binding["createdVia"] == "conversation-create" for binding in bindings)
                assert bindings[0]["agentSessionId"] != bindings[1]["agentSessionId"]
                for binding in bindings:
                    entry = native_store.lookup_by_session_key(binding["sessionKey"])
                    assert entry.session_id == binding["agentSessionId"]
                    assert native_store._db.get_messages(entry.session_id) == []
                assert all(account.conversations.list_messages(cid)["messages"] == [] for cid in ids)
            finally:
                account.close()
            assert seen == []
            assert not any(e["eventType"] == "conversation.command.result" for e in _events(core))
        finally:
            await adapter.disconnect()
    asyncio.run(scenario())


def test_aac_m4a_upload_reaches_real_native_audio_event_unchanged(native_store, tmp_path):
    from gateway.platforms.base import MessageType
    # A valid 100ms AAC/MPEG-4 recording (16kHz mono silence), as produced by
    # Android MediaRecorder MPEG_4 + AAC. No ffmpeg dependency at test time.
    recording = (Path(__file__).parent / "fixtures" / "voice-aac.m4a").read_bytes()
    digest = hashlib.sha256(recording).hexdigest()
    async def scenario():
        key = Ed25519PrivateKey.generate()
        core, bundle, cid, exposure = _gateway(tmp_path, key)
        adapter = _adapter(core, exposure)
        adapter.set_session_store(native_store)
        captured, entered = [], asyncio.Event()
        async def handler(event):
            path = Path(event.media_urls[0])
            captured.append((event, path, path.read_bytes()))
            entered.set()
        adapter.set_message_handler(handler)
        assert await adapter.connect()
        base = f"http://127.0.0.1:{adapter._site._server.sockets[0].getsockname()[1]}"
        try:
            async with ClientSession() as client:
                created = await _request(client, base, bundle, key, "POST", PREFIX + "/attachments", {
                    "clientAttachmentId": "ca_voice", "filename": "voice_recording.m4a", "mediaType": "audio/mp4",
                    "sizeBytes": len(recording), "sha256": digest,
                }, "req_voice_attachment")
                aid = created["data"]["attachment"]["attachmentId"]
                target = f"{PREFIX}/attachments/{aid}/content"
                _, headers = _signed_request(bundle, key, "PUT", target, request_id="req_voice_upload")
                preimage = "\n".join(("OPEN-ANDROID-INTELLIGENCE-REQUEST-V2", "PUT", target, ACCOUNT_ID,
                    bundle["deviceId"], bundle["sessionId"], headers["X-Open-Android-Intelligence-Request-Id"],
                    headers["X-Open-Android-Intelligence-Timestamp"], headers["X-Open-Android-Intelligence-Nonce"], digest)).encode("ascii")
                headers["X-Open-Android-Intelligence-Signature"] = _b64url(key.sign(preimage))
                headers.update({"Content-Type": "audio/mp4", "Content-Length": str(len(recording)),
                    "Digest": "sha-256=" + base64.b64encode(hashlib.sha256(recording).digest()).decode("ascii")})
                async with client.put(base + target, data=recording, headers=headers) as response:
                    assert response.status == 200, await response.json()
                await _request(client, base, bundle, key, "POST", f"{PREFIX}/attachments/{aid}/commit", {}, "req_voice_commit")
                await _request(client, base, bundle, key, "POST", f"{PREFIX}/conversations/{cid}/messages", {
                    "clientMessageId": "cm_voice", "text": "", "attachments": [{"attachmentId": aid}],
                }, "req_voice_message")
                await asyncio.wait_for(entered.wait(), 5)
                async with asyncio.timeout(5):
                    while adapter._session_tasks:
                        await asyncio.sleep(0.01)
            event, path, content = captured[0]
            assert event.message_type == MessageType.AUDIO
            assert event.media_types == ["audio/mp4"]
            assert event.text == ""
            assert path.suffix == ".m4a"
            assert content == recording
            assert not path.exists(), "The private decoded audio cache is removed only after native completion"
            statuses = [e["payload"]["status"] for e in _events(core)
                if e["eventType"] == "conversation.message.status"]
            assert statuses == ["queued", "delivered", "completed"]
        finally:
            await adapter.disconnect()
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_body", [None,
    {"clientMessageId": "cm_native_cancel_identity"},
    {"requestId": "req_cancel"},
], ids=["complete", "canonical-cancel", "legacy-cancel"])
def test_real_scoped_handler_finishes_whole_turn_and_closes_every_progress_message(native_store, tmp_path, cancel_body):
    from gateway.run_adapters import GatewayAdapterLifecycleMixin

    async def scenario():
        key = Ed25519PrivateKey.generate()
        core, bundle, cid, exposure = _gateway(tmp_path, key)
        adapter = _adapter(core, exposure)
        adapter.set_session_store(native_store)
        entered, release = asyncio.Event(), asyncio.Event()
        class Runner(GatewayAdapterLifecycleMixin):
            def __init__(self):
                self.interrupts = []
            def _standalone_launch_scope(self):
                return nullcontext()
            async def handle(self, event):
                account = core.open_gateway_account(ACCOUNT_ID)
                try:
                    session_id = account.agent_sessions.lookup(cid)["agentSessionId"]
                finally:
                    account.close()
                native_store.append_to_transcript(session_id, {
                    "role": "user", "content": event.text, "message_id": event.message_id,
                })
                await adapter.send(cid, "Planning", metadata={"expect_edits": True, "messageId": "msg_plan"})
                await adapter.edit_message(cid, "msg_plan", "Plan ready", finalize=True)
                await adapter.send(cid, "Working", metadata={"expect_edits": True, "messageId": "msg_work"})
                await adapter.send(cid, "Checking", metadata={"expect_edits": True, "messageId": "msg_check"})
                entered.set()
                await release.wait()
                return "Final answer"
            async def _interrupt_and_clear_session(self, session_key, source, **kwargs):
                self.interrupts.append((session_key, source.chat_id, kwargs))
        runner = Runner()
        # Use the actual host wrapper responsible for losing __self__.
        wrapped = runner._standalone_scoped(runner.handle)
        assert getattr(wrapped, "__self__", None) is None
        adapter.set_message_handler(wrapped)
        assert adapter._host_runner() is runner
        assert await adapter.connect()
        assert core.generation_canceller is not None
        base = f"http://127.0.0.1:{adapter._site._server.sockets[0].getsockname()[1]}"
        try:
            async with ClientSession() as client:
                offer = _negotiation_body(core.contracts.core_schema_hash)
                offer["features"]["conversationUi"].append("generation-cancel-v1")
                negotiated = await _request(client, base, bundle, key, "POST", PREFIX + "/negotiate", offer, "req_lifecycle_capability")
                assert "generation-cancel-v1" in negotiated["data"]["features"]["conversationUi"]
                receipt = await _request(client, base, bundle, key, "POST", f"{PREFIX}/conversations/{cid}/messages",
                    {"clientMessageId": "cm_lifecycle", "text": "Do some work", "attachments": []}, "req_lifecycle")
                gid = receipt["data"]["message"]["generationId"]
                await asyncio.wait_for(entered.wait(), 5)
                current = await _request(client, base, bundle, key, "GET", f"{PREFIX}/conversations/{cid}/generations/current", request_id="req_current")
                assert current["data"]["generation"]["state"] == "running"
                assert not any(e["payload"].get("status") in {"completed", "failed"} for e in _events(core))
                if cancel_body is not None:
                    target = f"{PREFIX}/conversations/{cid}/generations/{gid}/cancel"
                    result = await _request(client, base, bundle, key, "POST", target, cancel_body, "req_cancel")
                    assert result["data"]["outcome"] == "CANCELLED"
                    replay = await _request(client, base, bundle, key, "POST", target, cancel_body, "req_cancel")
                    assert replay["data"] == result["data"]
                    assert len(runner.interrupts) == 1
                    assert runner.interrupts[0][2]["invalidation_reason"] == "gateway_generation_cancel"
                    assert not adapter._session_tasks
                else:
                    release.set()
                async with asyncio.timeout(5):
                    while adapter._session_tasks:
                        await asyncio.sleep(0.01)
            events = _events(core)
            deltas = [e for e in events if e["eventType"] == "conversation.message.delta"]
            assert {e["payload"]["generationId"] for e in deltas} == {gid}
            completed = [e for e in events if e["eventType"] == "conversation.message.completed"]
            assert {"msg_plan", "msg_work", "msg_check"} <= {e["payload"]["messageId"] for e in completed}
            statuses = [e for e in events if e["eventType"] == "conversation.message.status"]
            assert statuses[-1]["payload"]["status"] == ("failed" if cancel_body is not None else "completed")
            assert statuses[-1]["payload"]["generationId"] == gid
            terminal_index = events.index(statuses[-1])
            assert all(events.index(e) < terminal_index for e in completed)
            assert adapter._streaming_replies == {}
            before = len(events)
            # A late callback with the same turn context cannot revive output.
            await adapter._publish_event("conversation.message.delta", cid, "msg_late", "stale",
                account_id=ACCOUNT_ID, generation_id=gid)
            assert len(_events(core)) == before
            from open_android_intelligence_gateway.device_tools import trusted_turn
            token = trusted_turn.set({"core": core, "accountId": ACCOUNT_ID,
                "conversationId": cid, "messageId": receipt["data"]["message"]["messageId"]})
            try:
                await adapter.send(cid, "Late output from the old task")
                assert len(_events(core)) == before
            finally:
                trusted_turn.reset(token)
            await adapter.send(cid, "Independent reminder")
            reminder = _events(core)[-1]
            assert reminder["payload"]["text"] == "Independent reminder"
            assert "generationId" not in reminder["payload"]
            adapter.set_message_handler(None)
            assert core.generation_canceller is None
        finally:
            release.set()
            await adapter.disconnect()
    asyncio.run(scenario())


def test_catalog_comes_from_live_native_registry_and_updates_after_registration(native_store, tmp_path, monkeypatch):
    from hermes_cli import commands as host
    from open_android_intelligence_gateway.core import create_gateway_core
    from test_support import make_secret_store
    core = create_gateway_core(tmp_path, secret_store=make_secret_store())
    first = core.command_catalog_response("en")
    commands = {c["invocation"]: c for c in first["commands"]}
    gates = getattr(host, "_resolve_config_gates", lambda: set())()
    for cmd in host.COMMAND_REGISTRY:
        if host._is_gateway_available(cmd, gates):
            assert "/" + cmd.name in commands
        elif cmd.cli_only:
            assert "/" + cmd.name not in commands
    assert "/models" not in commands  # The fabricated old catalog named this; Hermes serves /model.
    monkeypatch.setattr(host, "COMMAND_REGISTRY", [*host.COMMAND_REGISTRY,
        host.CommandDef("fresh-command", "Added by the running host", "Info", args_hint="[query]")])
    second = core.command_catalog_response("en")
    added = next(c for c in second["commands"] if c["invocation"] == "/fresh-command")
    assert added["acceptsArguments"] is True
    assert added["argumentHint"] == "[query]"
    assert "command" not in added
    assert second["catalogVersion"] != first["catalogVersion"]
