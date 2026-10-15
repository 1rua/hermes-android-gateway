"""真实历史原件采用、签名流式响应及授权失效回归。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from open_android_intelligence_gateway.core import GatewayError, create_gateway_core, request_signature_preimage
from open_android_intelligence_gateway.history_media import HistoryMedia
from test_support import make_secret_store
from test_conversation_rename import ACCOUNT_ID, HOST_API, _b64url, _gateway, _timestamp_millis


@pytest.fixture
def original(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    path = home / "outputs" / "中文资料.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"data":"native original"}')
    core = create_gateway_core(tmp_path / "gateway", secret_store=make_secret_store())
    account = core.open_gateway_account("acct_media")
    cid = account.conversations.create("cc_media", "历史原件", "cor_media")["conversationId"]
    try:
        yield core, account, cid, path
    finally:
        account.close()


def test_original_survives_source_retirement_and_stable_retry(original):
    _core, account, cid, source = original
    media = HistoryMedia(account)
    part = media.register(cid, "msg_media", source)
    payload = source.read_bytes()
    source.rename(source.with_suffix(".retired"))
    assert media.metadata(cid, part["attachmentId"])["remoteAvailable"]
    assert media.register(cid, "msg_media", source)["attachmentId"] == part["attachmentId"]
    owned = Path(media._record(cid, part["attachmentId"])["path"])
    assert owned.read_bytes() == payload
    assert owned.is_relative_to(source.parents[1] / "history-media" / "open-android-intelligence")
    assert "path" not in part and "sourcePath" not in part


def test_large_original_uses_bounded_stream_and_legacy_limit(original):
    _core, account, cid, source = original
    chunk = b"q" * (64 * 1024)
    with source.open("wb") as output:
        for _ in range(600):
            output.write(chunk)
    media = HistoryMedia(account)
    part = media.register(cid, "msg_large", source)
    context = {"deviceId":"dev_media", "pairingGeneration":1, "grantRevision":1}
    assert not media.metadata(cid, part["attachmentId"])["remoteAvailable"]
    assert media.metadata(cid, part["attachmentId"], format="stream-v1")["remoteAvailable"]
    grant = media.grant(cid, part["attachmentId"], context, format="stream-v1")
    stream = media.content_stream(cid, part["attachmentId"], grant["grantId"], context)
    try:
        sizes = [len(data) for data in stream]
    finally:
        stream.close()
    assert sum(sizes) == 600 * len(chunk)
    assert max(sizes) <= 64 * 1024


def signed(bundle, target, method, key, request_id, body=None):
    raw = json.dumps(body, separators=(",", ":")).encode() if body is not None else b""
    timestamp = _timestamp_millis()
    nonce = _b64url(hashlib.sha256(request_id.encode()).digest()[:16])
    preimage = request_signature_preimage({"method":method,"target":target,"accountId":ACCOUNT_ID,
        "deviceId":bundle["deviceId"],"sessionId":bundle["sessionId"],"requestId":request_id,
        "timestamp":timestamp,"nonce":nonce,"bodyHex":raw.hex()})
    headers = {"Authorization":"Bearer "+bundle["accessToken"], "X-Open-Android-Intelligence-Protocol":"2.1",
        "X-Open-Android-Intelligence-Account":ACCOUNT_ID,"X-Open-Android-Intelligence-Device":bundle["deviceId"],
        "X-Open-Android-Intelligence-Session":bundle["sessionId"],"X-Open-Android-Intelligence-Request-Id":request_id,
        "X-Open-Android-Intelligence-Timestamp":timestamp,"X-Open-Android-Intelligence-Nonce":nonce,
        "X-Open-Android-Intelligence-Signature":_b64url(key.sign(preimage)),"Idempotency-Key":request_id,
        "Content-Type":"application/json"}
    return {"method":method,"url":target,"headers":headers,"body":raw}


def test_real_http_streams_json_original_and_rejects_unsigned_format(tmp_path, monkeypatch):
    aiohttp = pytest.importorskip("aiohttp")
    from open_android_intelligence_gateway.adapter import OpenAndroidPlatformAdapter
    from open_android_intelligence_gateway.admin import create_admin_service
    from open_android_intelligence_gateway.plugin import GatewayServices
    from test_streaming_attachment_transport import _Config

    async def scenario():
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        source = tmp_path / "home" / "outputs" / "raw.json"
        source.parent.mkdir(parents=True)
        payload = b'{"error":"this is the actual attachment, not an envelope"}' * (700 * 1024)
        source.write_bytes(payload)
        key = Ed25519PrivateKey.generate()
        core, bundle, cid, exposure = _gateway(tmp_path / "gateway", key)
        account = core.open_gateway_account(ACCOUNT_ID)
        try:
            part = HistoryMedia(account).register(cid, "msg_json", source)
        finally:
            account.close()
        adapter = OpenAndroidPlatformAdapter(_Config(), GatewayServices(core,
            create_admin_service(core=core, host_version="1.0.0", host_api=HOST_API), exposure))
        assert await adapter.connect()
        try:
            port = adapter._site._server.sockets[0].getsockname()[1]
            base = f"http://127.0.0.1:{port}"
            path = f"/open-android-intelligence/v2/conversations/{cid}/attachments/{part['attachmentId']}"
            async with aiohttp.ClientSession() as client:
                counter = 0
                async def send(method, suffix, body=None, signed_suffix=None):
                    nonlocal counter
                    counter += 1
                    req = signed(bundle,path+(signed_suffix or suffix),method,key,f"req_stream_{counter}",body)
                    return await client.request(method,base+path+suffix,data=req["body"],headers=req["headers"])
                async with await send("GET", "/metadata?format=stream-v1") as response:
                    assert response.status == 200
                    metadata = (await response.json())["data"]["metadata"]
                    assert metadata["remoteAvailable"] and metadata["sizeBytes"] == len(payload)
                async with await send("POST", "/cache-grant?format=stream-v1", {}) as response:
                    token = (await response.json())["data"]["grantId"]
                suffix = f"/content?format=stream-v1&grantId={token}"
                async with await send("GET", suffix) as response:
                    assert response.status == 200
                    assert response.headers["X-Open-Android-Intelligence-Media-Format"] == "stream-v1"
                    assert response.headers["Content-Type"] == "application/json"
                    assert response.headers["Content-Length"] == str(len(payload))
                    assert response.headers["Digest"] == "sha-256="+base64.b64encode(hashlib.sha256(payload).digest()).decode()
                    digest, count = hashlib.sha256(), 0
                    async for data in response.content.iter_chunked(64 * 1024):
                        digest.update(data)
                        count += len(data)
                    assert count == len(payload) and digest.digest() == hashlib.sha256(payload).digest()
                async with await send("GET", suffix) as response:
                    assert response.status == 401
                async with await send("GET", "/metadata?format=stream-v1", signed_suffix="/metadata") as response:
                    assert response.status == 401
        finally:
            await adapter.disconnect()
    asyncio.run(scenario())


@pytest.mark.parametrize("suffix", ["?format=", "?format=other", "?format=stream-v1&format=stream-v1", "?extra=1", "?format=stream-v1&grantId=x"])
@pytest.mark.parametrize("operation,method,body", [("metadata","GET",None),("cache-grant","POST",{})])
def test_signed_query_is_closed(tmp_path, monkeypatch, suffix, operation, method, body):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    key = Ed25519PrivateKey.generate()
    _core, bundle, cid, exposure = _gateway(tmp_path / "gateway", key)
    target = f"/open-android-intelligence/v2/conversations/{cid}/attachments/media_unknown/{operation}{suffix}"
    response = exposure.routes[-1]._handle_raw(signed(bundle,target,method,key,"req_query",body))
    assert response["body"]["error"]["code"] == "SCHEMA_INVALID"


def test_grant_format_expiry_and_midstream_deletion_are_enforced(original):
    _core, account, cid, source = original
    source.write_bytes(b"x" * (3 * 64 * 1024))
    media, context = HistoryMedia(account), {"deviceId":"dev_media", "pairingGeneration":1, "grantRevision":1}
    part = media.register(cid, "msg_checks", source)
    now = datetime.now(timezone.utc)
    token = media.grant(cid, part["attachmentId"], context, now, format="stream-v1")["grantId"]
    with pytest.raises(GatewayError, match="AUTHENTICATION_FAILED"):
        media.content(cid, part["attachmentId"], token, context, now)
    with pytest.raises(GatewayError, match="AUTHENTICATION_FAILED"):
        media.content_stream(cid, part["attachmentId"], token, context, now+timedelta(seconds=121))
    stream = media.content_stream(cid, part["attachmentId"], token, context, now)
    assert len(next(stream)) == 64 * 1024
    media.mark_message_deleted("msg_checks", cid)
    with pytest.raises(GatewayError, match="ATTACHMENT_NOT_FOUND"):
        next(stream)
    assert stream.closed
    with pytest.raises(GatewayError, match="ATTACHMENT_NOT_FOUND"):
        media.register(cid, "msg_checks", source)


def test_equal_length_owned_mutation_closes_existing_stream(original):
    _core, account, cid, source = original
    source.write_bytes(b"x" * (3 * 64 * 1024))
    media, context = HistoryMedia(account), {"deviceId":"dev_media", "pairingGeneration":1, "grantRevision":1}
    part = media.register(cid, "msg_tamper", source)
    token = media.grant(cid, part["attachmentId"], context, format="stream-v1")["grantId"]
    stream = media.content_stream(cid, part["attachmentId"], token, context)
    assert next(stream) == b"x" * (64 * 1024)
    owned = Path(media._record(cid, part["attachmentId"])["path"])
    owned.write_bytes(b"y" * owned.stat().st_size)
    with pytest.raises(GatewayError, match="ATTACHMENT_READ_FAILED"):
        next(stream)
    assert stream.closed and not media.metadata(cid, part["attachmentId"], format="stream-v1")["remoteAvailable"]


def test_legacy_surviving_source_migrates_in_place_and_missing_source_stays_visible(original):
    _core, account, cid, source = original
    media = HistoryMedia(account)
    record = {"attachmentId":"media_legacy", "conversationId":cid, "messageId":"msg_legacy", "filename":source.name,
        "mediaType":"application/json", "sizeBytes":source.stat().st_size,
        "sha256":"sha256:"+hashlib.sha256(source.read_bytes()).hexdigest(), "path":str(source)}
    media._put("history-media:media_legacy", record)
    assert media.metadata(cid,"media_legacy",format="stream-v1")["remoteAvailable"]
    source.rename(source.with_suffix(".retired"))
    assert media.register(cid,"msg_legacy",source)["attachmentId"] == "media_legacy"
    media._put("history-media:media_missing", dict(record,attachmentId="media_missing",messageId="msg_missing"))
    metadata = media.metadata(cid,"media_missing",format="stream-v1")
    assert metadata["status"] == "REMOTE_UNAVAILABLE" and metadata["filename"] == source.name


@pytest.mark.parametrize("media_type", ["text/plain\r\nX-Evil: yes", "application/\x00json", "text/\x7fplain", "中文/类型"])
def test_invalid_original_http_type_is_refused_before_publication(original, media_type):
    _core, account, cid, source = original
    with pytest.raises(GatewayError, match="SCHEMA_INVALID"):
        HistoryMedia(account).register(cid,"msg_mime",source,media_type=media_type)


@pytest.mark.parametrize("change", [{"deviceId":"dev_other"},{"pairingGeneration":2},{"grantRevision":2}])
def test_stream_grant_keeps_all_verified_scope_fields(original, change):
    _core, account, cid, source = original
    media, context = HistoryMedia(account), {"deviceId":"dev_media", "pairingGeneration":1, "grantRevision":1}
    part = media.register(cid,"msg_scope",source)
    token = media.grant(cid,part["attachmentId"],context,format="stream-v1")["grantId"]
    with pytest.raises(GatewayError,match="AUTHENTICATION_FAILED"):
        media.content_stream(cid,part["attachmentId"],token,context|change)


@pytest.mark.parametrize("lifecycle", ["grant_revision", "pairing_generation", "session_revoked", "account_deleted"])
def test_current_lifecycle_is_rechecked_between_stream_chunks(tmp_path, monkeypatch, lifecycle):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    source = tmp_path / "home" / "outputs" / "lifecycle.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"x" * (3 * 64 * 1024))
    key = Ed25519PrivateKey.generate()
    core, bundle, cid, exposure = _gateway(tmp_path / "gateway", key)
    account = core.open_gateway_account(ACCOUNT_ID)
    try:
        media = HistoryMedia(account)
        part = media.register(cid,"msg_lifecycle",source)
        context = {"deviceId":bundle["deviceId"],"pairingGeneration":1,"grantRevision":1}
        grant = media.grant(cid,part["attachmentId"],context,format="stream-v1")
    finally:
        account.close()
    target = f"/open-android-intelligence/v2/conversations/{cid}/attachments/{part['attachmentId']}/content?format=stream-v1&grantId={grant['grantId']}"
    response = exposure.routes[-1]._handle_raw(signed(bundle,target,"GET",key,"req_lifecycle"))
    stream = response["stream"]
    assert next(stream) == b"x" * (64 * 1024)
    account = core.open_gateway_account(ACCOUNT_ID)
    try:
        if lifecycle == "session_revoked":
            account.sessions.revoke_session(bundle["sessionId"],"cor_revoke")
        elif lifecycle == "account_deleted":
            # 模拟既有账号目录被管理操作移出；不要创建同名新账号。
            account.paths.root.rename(account.paths.root.with_name(account.paths.root.name+"-retired"))
        else:
            account.store.database.execute(f"UPDATE device_keys SET {lifecycle}={lifecycle}+1 WHERE device_id=?",(bundle["deviceId"],))
    finally:
        account.close()
    with pytest.raises(GatewayError,match="AUTHENTICATION_FAILED"):
        next(stream)
    assert stream.closed


def test_outbound_delivery_publishes_only_the_adopted_owned_original(tmp_path, monkeypatch):
    from open_android_intelligence_gateway.adapter import OpenAndroidPlatformAdapter
    from open_android_intelligence_gateway.admin import create_admin_service
    from open_android_intelligence_gateway.plugin import GatewayServices
    from test_streaming_attachment_transport import _Config
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    source = tmp_path / "home" / "cache" / "outbound" / "原始附件.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"native outbound original")
    key = Ed25519PrivateKey.generate()
    core, _bundle, cid, exposure = _gateway(tmp_path / "gateway", key)
    adapter = OpenAndroidPlatformAdapter(_Config(),GatewayServices(core,
        create_admin_service(core=core,host_version="1.0.0",host_api=HOST_API),exposure))
    result = asyncio.run(adapter.send_document(cid,str(source),caption="合成说明"))
    assert result.success
    source.parent.rename(source.parent.with_name("outbound-retired"))
    account = core.open_gateway_account(ACCOUNT_ID)
    try:
        events = account.events.read_after(None)
        event = next(event for event in events if event["eventType"]=="conversation.message.completed")
        part = next(part for part in event["payload"]["parts"] if part["type"]=="attachment")
        assert HistoryMedia(account).metadata(cid,part["attachmentId"],format="stream-v1")["remoteAvailable"]
        assert part["sha256"] == "sha256:"+hashlib.sha256(b"native outbound original").hexdigest()
    finally:
        account.close()


def test_host_route_writes_bounded_bytes_and_terminates_on_lifecycle_change(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    source = tmp_path / "home" / "outputs" / "host-route.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"x" * (3 * 64 * 1024))
    key = Ed25519PrivateKey.generate()
    core, bundle, cid, exposure = _gateway(tmp_path / "gateway", key)
    account = core.open_gateway_account(ACCOUNT_ID)
    try:
        media = HistoryMedia(account)
        part = media.register(cid,"msg_host_route",source)
        token = media.grant(cid,part["attachmentId"],{"deviceId":bundle["deviceId"],"pairingGeneration":1,"grantRevision":1},format="stream-v1")["grantId"]
    finally:
        account.close()
    class Response:
        def __init__(self):
            self.headers, self.chunks, self.destroyed, self.ended = {}, [], False, False
        def set_header(self,name,value):
            self.headers[name] = value
        def write(self,chunk):
            assert isinstance(chunk,bytes)
            self.chunks.append(chunk)
            current = core.open_gateway_account(ACCOUNT_ID)
            try:
                current.sessions.revoke_session(bundle["sessionId"],"cor_stop_host_route")
            finally:
                current.close()
        def end(self):
            self.ended = True
        def destroy(self):
            self.destroyed = True
    target = f"/open-android-intelligence/v2/conversations/{cid}/attachments/{part['attachmentId']}/content?format=stream-v1&grantId={token}"
    response = Response()
    assert exposure.routes[-1].handler(signed(bundle,target,"GET",key,"req_host_route"),response)
    assert response.headers["Content-Length"] == str(source.stat().st_size)
    assert response.chunks == [b"x" * (64 * 1024)]
    assert response.destroyed and not response.ended
