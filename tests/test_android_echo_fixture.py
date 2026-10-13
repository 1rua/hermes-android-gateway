"""独立子进程运行 Android 互通夹具，检查启动、HTTP 入口与安全结束。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from aiohttp import ClientSession
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from test_batch_http_dispatch import BATCH, PREFIX, _b64url, _next_event, _request, _signed_request
from test_support import contract_root, core_schema_hash


@contextmanager
def _fixture_process(tmp_path, echo_agent, *, stream_agent=False, history_agent=False):
    log_path = tmp_path / "fixture-stderr.txt"
    env = dict(os.environ)
    env.pop("OAI_GATEWAY_MASTER_KEY_FILE", None)
    env["HERMES_HOME"] = str(tmp_path / "inherited-hermes-home")
    env["HERMES_TEST_ISOLATION"] = env["HERMES_HOME"]
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    fixture = Path(__file__).with_name("android_gateway_fixture.py")
    command = [sys.executable, str(fixture), "--contract-root", str(contract_root())]
    if echo_agent:
        command.append("--echo-agent")
    if echo_agent or stream_agent or history_agent:
        # 此路径不存在；若 echo 误用继承的线上密钥路径，启动必须失败。
        for name in ("OAI_GATEWAY_MASTER_KEY_FILE", "OPEN_ANDROID_INTELLIGENCE_GATEWAY_MASTER_KEY_FILE", "OPEN_ANDROID_GATEWAY_MASTER_KEY_FILE"):
            env[name] = str(tmp_path / "inherited-master-key")
        env["OPEN_ANDROID_GATEWAY_PORT"] = "11451"
        env["OPEN_ANDROID_GATEWAY_HOST"] = "203.0.113.10"
        env["OPEN_ANDROID_ACCOUNT_ID"] = "inherited-account"
    if stream_agent:
        command.append("--stream-agent")
    if history_agent:
        command.append("--history-agent")
    with log_path.open("w") as stderr:
        process = subprocess.Popen(command, stdin=subprocess.PIPE if stream_agent else None, stdout=subprocess.PIPE, stderr=stderr, text=True, env=env)
        try:
            yield process, log_path
        finally:
            process.terminate()
            process.wait(timeout=10)
            process.stdout.close()
            if process.stdin is not None:
                process.stdin.close()


async def _fixture_url(process, log_path):
    first_line = await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), timeout=10)
    assert first_line.startswith("http://127.0.0.1:"), log_path.read_text()
    parsed = urlsplit(first_line.strip())
    assert parsed.hostname == "127.0.0.1" and parsed.port > 0
    return first_line.strip()


@pytest.mark.parametrize("echo_agent", [False, True])
def test_fixture_cli_starts_on_random_loopback_port_and_stops(tmp_path, echo_agent):
    async def scenario():
        with _fixture_process(tmp_path, echo_agent) as (process, log_path):
            base_url = await _fixture_url(process, log_path)
            async with ClientSession() as client:
                async with client.get(base_url + "/fixture-not-a-gateway-route") as response:
                    assert response.status == 404
        assert process.poll() is not None
        if echo_agent:
            assert not (tmp_path / "inherited-master-key").exists()
            assert not (tmp_path / "inherited-hermes-home").exists()

    asyncio.run(scenario())


def test_history_fixture_requires_explicit_native_sdk_before_importing_host(tmp_path):
    # 即使父进程已有宿主 PYTHONPATH，也不能掩盖历史联调缺少明确 SDK 来源。
    env = dict(os.environ)
    env.pop("HERMES_SDK_ROOT", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("android_gateway_fixture.py")), "--history-agent"],
        capture_output=True, text=True, env=env, timeout=10,
    )
    assert result.returncode != 0
    assert "HERMES_SDK_ROOT_REQUIRED" in result.stderr
    assert not result.stdout


def test_native_history_fixture_rejects_host_code_outside_explicit_sdk(tmp_path):
    # 指定 SDK 根必须约束真实模块来源，父进程的 PYTHONPATH 不能补成另一套宿主。
    sdk_root = Path(os.environ["HERMES_SDK_ROOT"]).resolve()
    incomplete_sdk = tmp_path / "database-only-sdk"
    incomplete_sdk.mkdir()
    (incomplete_sdk / "hermes_state.py").write_bytes((sdk_root / "hermes_state.py").read_bytes())
    env = dict(os.environ)
    env["HERMES_SDK_ROOT"] = str(incomplete_sdk)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(sdk_root), env.get("PYTHONPATH"))))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("android_gateway_fixture.py")), "--history-agent",
            "--contract-root", str(contract_root())],
        input="", capture_output=True, text=True, env=env, timeout=10,
    )
    assert result.returncode != 0
    assert "HERMES_NATIVE_SOURCE_MISMATCH" in result.stderr
    assert not result.stdout


def test_native_history_fixture_preserves_original_text_tools_and_owned_image_over_http(tmp_path):
    async def scenario():
        with _fixture_process(tmp_path, False, history_agent=True) as (process, log_path):
            base_url = await _fixture_url(process, log_path)
            image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
            digest = hashlib.sha256(image).hexdigest()
            key = Ed25519PrivateKey.generate()
            async with ClientSession() as client:
                bundle = await _login(client, base_url, key)
                created = await _request(client, base_url, bundle, key, "POST", PREFIX + "/conversations",
                    {"clientConversationId": "cc_fixture_history"}, "req_history_create")
                conversation_id = created["data"]["conversation"]["conversationId"]
                attachment = await _request(client, base_url, bundle, key, "POST", PREFIX + "/attachments", {
                    "clientAttachmentId": "ca_fixture_history", "filename": "synthetic.png", "mediaType": "image/png",
                    "sizeBytes": len(image), "sha256": digest,
                }, "req_history_upload_create")
                attachment_id = attachment["data"]["attachment"]["attachmentId"]
                upload_target = f"{PREFIX}/attachments/{attachment_id}/content"
                _, upload_headers = _signed_request(bundle, key, "PUT", upload_target, request_id="req_history_upload")
                preimage = "\n".join((
                    "OPEN-ANDROID-INTELLIGENCE-REQUEST-V2", "PUT", upload_target, "alice",
                    bundle["deviceId"], bundle["sessionId"], upload_headers["X-Open-Android-Intelligence-Request-Id"],
                    upload_headers["X-Open-Android-Intelligence-Timestamp"], upload_headers["X-Open-Android-Intelligence-Nonce"], digest,
                )).encode("ascii")
                upload_headers["X-Open-Android-Intelligence-Signature"] = _b64url(key.sign(preimage))
                upload_headers.update({"Content-Type": "image/png", "Content-Length": str(len(image)),
                    "Digest": "sha-256=" + base64.b64encode(hashlib.sha256(image).digest()).decode("ascii")})
                async with client.put(base_url + upload_target, headers=upload_headers, data=image) as response:
                    assert response.status == 200, await response.json()
                await _request(client, base_url, bundle, key, "POST", f"{PREFIX}/attachments/{attachment_id}/commit", {}, "req_history_commit")

                _, headers = _signed_request(bundle, key, "GET", PREFIX + "/events", request_id="req_history_stream")
                headers["Accept"] = "text/event-stream"
                async with client.get(base_url + PREFIX + "/events", headers=headers) as stream:
                    assert stream.status == 200
                    await _request(client, base_url, bundle, key, "GET", PREFIX + "/sync/snapshot", request_id="req_history_baseline")
                    while True:
                        event = await _next_event(stream)
                        if event["event"] == "gateway.notice" and event["payload"]["noticeCode"] == "SYNC_BASELINE":
                            break
                    original = "请说明这张合成图片\n保留这个原始文本。"
                    receipt = await _request(client, base_url, bundle, key, "POST", f"{PREFIX}/conversations/{conversation_id}/messages", {
                        "clientMessageId": "cm_fixture_history", "text": original, "attachments": [{"attachmentId": attachment_id}],
                    }, "req_history_message")
                    published = []
                    while True:
                        event = await _next_event(stream)
                        if event["event"] == "conversation.message.completed":
                            published.append(event["payload"])
                        if event["event"] == "conversation.message.status" and event["payload"]["clientMessageId"] == "cm_fixture_history" and event["payload"]["status"] in {"completed", "failed"}:
                            assert event["payload"]["status"] == "completed"
                            break
                    assert [item["text"] for item in published] == ["正在查询合成测试图片信息", "这是一张合成测试图片，原始附件已经收到。"]

                switched = await _request(client, base_url, bundle, key, "POST", PREFIX + "/conversations", {
                    "clientConversationId": "cc_fixture_history_other",
                }, "req_history_other")
                other_id = switched["data"]["conversation"]["conversationId"]
                other = await _request(client, base_url, bundle, key, "GET", f"{PREFIX}/conversations/{other_id}/messages", request_id="req_history_other_read")
                assert other["data"]["messages"] == []
                for index in range(2):
                    reopened = await _request(client, base_url, bundle, key, "GET", f"{PREFIX}/conversations/{conversation_id}/messages", request_id=f"req_history_read_{index}")
                    rows = reopened["data"]["messages"]
                    user = next(row for row in rows if row.get("clientMessageId") == "cm_fixture_history")
                    assert user["messageId"] == receipt["data"]["message"]["messageId"]
                    assert user["text"] == original
                    assert user["parts"] == [{"type": "text", "text": original}, {
                        "type": "attachment", "attachmentId": attachment_id,
                        "filename": "synthetic.png", "mediaType": "image/png", "sizeBytes": len(image),
                    }]
                    for item in published:
                        restored = next(row for row in rows if row["messageId"] == item["messageId"])
                        assert restored["text"] == item["text"]
                        assert restored["timestamp"] == item["timestamp"]
                    assert all(row.get("text") or row.get("parts") for row in rows if row["sender"] == "assistant")

                media_target = f"{PREFIX}/conversations/{conversation_id}/attachments/{attachment_id}"
                metadata = await _request(client, base_url, bundle, key, "GET", media_target + "/metadata", request_id="req_history_metadata")
                media = metadata["data"]["metadata"]
                assert media["attachmentId"] == attachment_id and media["conversationId"] == conversation_id
                assert media["sha256"] == "sha256:" + digest and media["remoteAvailable"] is True
                grant = await _request(client, base_url, bundle, key, "POST", media_target + "/cache-grant", {}, "req_history_grant")
                content_target = media_target + "/content?grantId=" + grant["data"]["grantId"]
                content = await _request(client, base_url, bundle, key, "GET", content_target, request_id="req_history_content")
                assert content["data"]["mediaType"] == "image/png"
                assert base64.b64decode(content["data"]["contentBase64"]) == image
                for request_id, target, expected_status, expected_code in (
                    ("req_history_reuse", content_target, 401, "AUTHENTICATION_FAILED"),
                    ("req_history_cross", f"{PREFIX}/conversations/{other_id}/attachments/{attachment_id}/metadata", 400, "ATTACHMENT_NOT_FOUND"),
                ):
                    _, headers = _signed_request(bundle, key, "GET", target, request_id=request_id)
                    async with client.get(base_url + target, headers=headers) as response:
                        assert response.status == expected_status
                        assert (await response.json())["error"]["code"] == expected_code
                diagnostic_rows = [json.loads(line) for line in log_path.read_text().splitlines() if line.startswith("{")]
                assert {"fixtureMode": "history-agent", "host": "native"} in diagnostic_rows
                persisted = next(row for row in diagnostic_rows if row.get("fixtureEvent") == "history-persisted")
                assert persisted["nativeSessionDb"] and persisted["modelInputPersisted"] and persisted["imageSha256"] == digest
                released = next(row for row in diagnostic_rows if row.get("fixtureEvent") == "history-input-released")
                assert released["released"] is True
        assert not (tmp_path / "inherited-master-key").exists()
        assert not (tmp_path / "inherited-hermes-home").exists()

    asyncio.run(scenario())


async def _login(client, base_url, key):
    negotiation_id = "neg_fixture_cli"
    installation_id = "install_fixture_cli"
    hello = {
        "negotiationId": negotiation_id,
        "protocol": {"major": 2, "minor": 1},
        "client": {"installationId": installation_id, "appVersion": "fixture-test", "platform": "android", "platformApi": 35},
        "features": {
            "auth": ["password"], "messages": ["chat-v1"], "attachments": ["staged-sha256-v1"],
            "events": ["sse-cursor-v1"], "deviceRequests": ["risk-queue-v1"],
            "conversationUi": ["message-batches-v1", "newline-v1"],
        },
        "schemaHashes": {"core": core_schema_hash()},
    }
    async with client.post(base_url + PREFIX + "/negotiate", json=hello) as response:
        negotiated = await response.json()
        assert response.status == 200, negotiated
        assert "message-batches-v1" in negotiated["data"]["features"]["conversationUi"]
    login = {
        "negotiationId": negotiation_id, "username": "alice", "password": "android-fixture-only",
        "installation": {"installationId": installation_id, "displayName": "隔离夹具设备", "devicePublicKey": _b64url(key.public_key().public_bytes_raw())},
    }
    async with client.post(base_url + PREFIX + "/sessions/password", json=login) as response:
        result = await response.json()
        assert response.status == 200
        assert result["data"]["accountId"] == "alice"
        return result["data"]


@pytest.mark.parametrize("channel", ["sse", "websocket"])
def test_stream_fixture_requires_each_live_delta_to_be_observed_before_completion(tmp_path, channel):
    async def scenario():
        with _fixture_process(tmp_path, True, stream_agent=True) as (process, log_path):
            base_url = await _fixture_url(process, log_path)
            key = Ed25519PrivateKey.generate()
            async with ClientSession() as client:
                bundle = await _login(client, base_url, key)
                created = await _request(client, base_url, bundle, key, "POST", PREFIX + "/conversations", {"clientConversationId": "cc_fixture_stream"}, "req_stream_create")
                conversation_id = created["data"]["conversation"]["conversationId"]
                _, headers = _signed_request(bundle, key, "GET", PREFIX + "/events", request_id="req_fixture_stream")
                headers["Accept"] = "text/event-stream"
                connection = await client.ws_connect(base_url + PREFIX + "/events", headers=headers) if channel == "websocket" else await client.get(base_url + PREFIX + "/events", headers=headers)

                async def next_event():
                    if channel == "sse":
                        return await _next_event(connection)
                    async with asyncio.timeout(5):
                        frame = await connection.receive_json()
                        return {**json.loads(frame["data"]), "event": frame["event"]}

                try:
                    await _request(client, base_url, bundle, key, "POST", f"{PREFIX}/conversations/{conversation_id}/messages", {"clientMessageId": "cm_fixture_stream", "text": "流式测试", "attachments": []}, "req_stream_message")
                    events = []
                    for expected in ("fixture-agent-", "fixture-agent-reply:\n", "fixture-agent-reply:\n流式测试"):
                        event = await next_event()
                        while event["event"] != "conversation.message.delta":
                            event = await next_event()
                        assert event["payload"]["text"] == expected
                        events.append(event)
                        # 未确认之前不允许模型完成，确保三个观察来自真实的逐步投递。
                        process.stdin.write("continue\n")
                        process.stdin.flush()
                    event = await next_event()
                    while event["event"] != "conversation.message.completed":
                        event = await next_event()
                    assert event["payload"]["text"] == "fixture-agent-reply:\n流式测试"
                    assert {entry["payload"]["messageId"] for entry in [*events, event]} == {events[0]["payload"]["messageId"]}
                    status = await next_event()
                    while status["event"] != "conversation.message.status" or status["payload"]["status"] not in {"completed", "failed"}:
                        status = await next_event()
                    assert status["payload"]["status"] == "completed", log_path.read_text()
                finally:
                    if channel == "websocket":
                        await connection.close()
                    else:
                        connection.close()
            async with asyncio.timeout(5):
                while not any(json.loads(line).get("fixtureEvent") == "stream-transport" for line in log_path.read_text().splitlines() if line.startswith("{")):
                    await asyncio.sleep(0.01)
            transports = [json.loads(line)["transport"] for line in log_path.read_text().splitlines() if line.startswith("{") and json.loads(line).get("fixtureEvent") == "stream-transport"]
            assert transports == [channel]

    asyncio.run(scenario())


@pytest.mark.parametrize("echo_agent", [False, True])
def test_fixture_cli_password_login_batch_and_reply_semantics(tmp_path, echo_agent):
    async def scenario():
        with _fixture_process(tmp_path, echo_agent) as (process, log_path):
            base_url = await _fixture_url(process, log_path)
            key = Ed25519PrivateKey.generate()
            async with ClientSession() as client:
                bundle = await _login(client, base_url, key)
                created = await _request(client, base_url, bundle, key, "POST", PREFIX + "/conversations", {"clientConversationId": "cc_fixture_cli"}, "req_cli_create")
                conversation_id = created["data"]["conversation"]["conversationId"]
                _, headers = _signed_request(bundle, key, "GET", PREFIX + "/events", request_id="req_cli_stream")
                headers["Accept"] = "text/event-stream"
                async with client.get(base_url + PREFIX + "/events", headers=headers) as stream:
                    assert stream.status == 200
                    if echo_agent:
                        await _request(client, base_url, bundle, key, "GET", PREFIX + "/sync/snapshot", request_id="req_cli_snapshot")
                        while True:
                            event = await _next_event(stream)
                            if event["event"] == "gateway.notice" and event["payload"]["noticeCode"] == "SYNC_BASELINE":
                                break
                    else:
                        assert await stream.read() == b": fixture connected\n\n"
                    target = f"{PREFIX}/conversations/{conversation_id}/message-batches"
                    receipt = await _request(client, base_url, bundle, key, "POST", target, BATCH, "req_cli_batch")
                    members = receipt["data"]["members"]
                    assert len({member["messageId"] for member in members}) == 2
                    if echo_agent:
                        seen = []
                        while sum(event["event"] == "conversation.message.status" and event["payload"]["status"] == "completed" for event in seen) < 2:
                            seen.append(await _next_event(stream))
                        replies = [event for event in seen if event["event"] == "conversation.message.completed"]
                        assert len(replies) == 1
                        assert replies[0]["payload"]["conversationId"] == conversation_id
                        assert replies[0]["payload"]["text"] == "fixture-agent-reply:\n  第一条\n\n🙂 第二条  "
                    replay = await _request(client, base_url, bundle, key, "POST", target, BATCH, "req_cli_batch")
                    assert replay == receipt
                    if echo_agent:
                        ordinary = {"clientMessageId": "cm_cli_regular", "text": "后续普通消息", "attachments": []}
                        await _request(client, base_url, bundle, key, "POST", f"{PREFIX}/conversations/{conversation_id}/messages", ordinary, "req_cli_regular")
                        while True:
                            event = await _next_event(stream)
                            if event["event"] == "conversation.message.completed":
                                assert event["payload"]["text"] == "fixture-agent-reply:\n后续普通消息"
                                break
        diagnostic_rows = [json.loads(line) for line in log_path.read_text().splitlines() if line.startswith("{")]
        if echo_agent:
            turns = [row for row in diagnostic_rows if row.get("fixtureEvent") == "agent-turn"]
            assert [row["clientMessageId"] for row in turns] == ["cm_http_first", "cm_cli_regular"]
            assert "hook failed" not in log_path.read_text()
            assert "Inbound dispatch failed" not in log_path.read_text()
            mode = next(row for row in diagnostic_rows if row.get("fixtureMode") == "echo-agent")
            if os.environ.get("OAI_REQUIRE_NATIVE_HERMES") == "1":
                assert mode["host"] == "native"
        else:
            assert not any(row.get("fixtureEvent") == "agent-turn" for row in diagnostic_rows)

    asyncio.run(scenario())
