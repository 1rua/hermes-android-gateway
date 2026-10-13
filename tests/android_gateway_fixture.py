"""Android HTTP 互通夹具，账号和服务均与用户 Gateway 隔离。

默认模式沿用 Core、HTTP 路由、Schema、密码存储与 Ed25519 验签；
echo 模式额外运行生产平台适配器、真实临时 AES-GCM 密钥与确定性测试 Agent。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import signal
import sys
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import mkdtemp
from types import SimpleNamespace

# 先隔离宿主环境，再导入可能读取宿主配置、日志与数据库位置的模块。
# 默认夹具分支保留原来的环境行为，显式 echo/stream/history 模式均强制隔离。
ECHO_STORAGE_ROOT = Path(mkdtemp(prefix="oai-android-echo-")) if any(
    flag in sys.argv for flag in ("--echo-agent", "--stream-agent", "--history-agent")
) else None
if ECHO_STORAGE_ROOT is not None:
    echo_home = str(ECHO_STORAGE_ROOT / "hermes-home")
    echo_key = str(ECHO_STORAGE_ROOT / "keys" / "gateway-master-key")
    os.environ["HERMES_HOME"] = echo_home
    os.environ["HERMES_TEST_ISOLATION"] = echo_home
    for name in ("OAI_GATEWAY_MASTER_KEY_FILE", "OPEN_ANDROID_INTELLIGENCE_GATEWAY_MASTER_KEY_FILE", "OPEN_ANDROID_GATEWAY_MASTER_KEY_FILE"):
        os.environ[name] = echo_key
    os.environ["OPEN_ANDROID_GATEWAY_HOST"] = "127.0.0.1"
    os.environ["OPEN_ANDROID_GATEWAY_PORT"] = "0"
    os.environ["OPEN_ANDROID_ACCOUNT_ID"] = "alice"

if any(flag in sys.argv for flag in ("--echo-agent", "--stream-agent", "--history-agent")):
    # SDK 根由调用方指定；相对路径以夹具进程的工作目录为基准。
    # 必须在隔离完成后、导入任何宿主模块前定位真实源码，缺失时直接失败。
    sdk_root = os.environ.get("HERMES_SDK_ROOT")
    if "--history-agent" in sys.argv and not sdk_root:
        raise RuntimeError("HERMES_SDK_ROOT_REQUIRED")
    if sdk_root:
        sdk_path = Path(sdk_root).expanduser().resolve()
        if not (sdk_path / "hermes_state.py").is_file():
            raise RuntimeError("HERMES_SDK_SOURCE_MISSING")
        sys.path.insert(0, str(sdk_path))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from open_android_intelligence_gateway.adapter import AccountPasswordVerifier, create_gateway_request_verifier
from open_android_intelligence_gateway.admin import HostApiCompatibility, create_admin_service
from open_android_intelligence_gateway.contract_assets import resolve_contract_root
from open_android_intelligence_gateway.core import create_gateway_core
from open_android_intelligence_gateway.http import create_gateway_exposure
from open_android_intelligence_gateway.local_keys import LocalMasterKeyStore, create_master_key_file
from test_support import make_secret_store

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
ECHO_REPLY_PREFIX = "fixture-agent-reply:\n"
HISTORY_PROGRESS = "正在查询合成测试图片信息"
HISTORY_REPLY = "这是一张合成测试图片，原始附件已经收到。"


def contract_root(explicit):
    """The contract this fixture serves, or a stated reason it cannot start."""
    if explicit:
        root = Path(explicit).expanduser()
        if not root.is_dir():
            raise SystemExit(f"--contract-root 指向的目录不存在: {root}")
        return root
    resolution = resolve_contract_root(PLUGIN_ROOT)
    if resolution.root is None:
        raise SystemExit(f"协议契约未就绪，无法启动 fixture：{resolution.reason}")
    return resolution.root


async def run_echo_agent(core, admin, exposure, port, *, stream_agent=False, history_agent=False):
    """测试专用确定性 Agent，经生产适配器完成消息回合与 SSE 回复。"""
    from open_android_intelligence_gateway.adapter import BasePlatformAdapter, OpenAndroidPlatformAdapter
    from open_android_intelligence_gateway.platform_identity import GATEWAY_PLATFORM_ID, GATEWAY_PLATFORM_IDS
    from open_android_intelligence_gateway.plugin import GatewayServices

    native_host = BasePlatformAdapter.__module__ == "gateway.platforms.base"
    if (history_agent or os.environ.get("OAI_REQUIRE_NATIVE_HERMES") == "1") and not native_host:
        raise RuntimeError("本次互通必须加载真实 Hermes BasePlatformAdapter")

    def require_sdk_source(symbol):
        if not sdk_root:
            return
        module = sys.modules.get(symbol.__module__)
        source = getattr(module, "__file__", None)
        if not source or not Path(source).resolve().is_relative_to(sdk_path):
            raise RuntimeError("HERMES_NATIVE_SOURCE_MISMATCH")

    if native_host:
        require_sdk_source(BasePlatformAdapter)
        from gateway.config import PlatformConfig
        from gateway.platform_registry import PlatformEntry, platform_registry

        # 真实 SessionStore 查找兼容来源时也会构造平台值，前置条件与生产注册一致。
        platform_ids = GATEWAY_PLATFORM_IDS
        for platform_id in platform_ids:
            platform_registry.register(PlatformEntry(
                name=platform_id, label="Android 隔离互通夹具",
                adapter_factory=OpenAndroidPlatformAdapter, check_fn=lambda: True,
            ))
        config = PlatformConfig(extra={"host": "127.0.0.1", "account_id": "alice"})
    else:
        config = SimpleNamespace(extra={"host": "127.0.0.1", "account_id": "alice"})
    adapter = OpenAndroidPlatformAdapter(config, GatewayServices(core, admin, exposure))
    adapter._port = port
    session_store = None
    if native_host:
        from gateway.config import GatewayConfig
        from gateway.session import SessionStore
        from hermes_state import SessionDB
        from open_android_intelligence_gateway.host_history import HermesHistoryPort

        require_sdk_source(SessionStore)
        require_sdk_source(SessionDB)
        session_store = SessionStore(Path(os.environ["HERMES_HOME"]) / "sessions", GatewayConfig())
        if not isinstance(session_store._db, SessionDB):
            raise RuntimeError("HERMES_NATIVE_SESSION_DB_REQUIRED")
        core.history_reader = HermesHistoryPort(core, session_store).read
        adapter.set_session_store(session_store)
    step_reader = None
    step_transport = None
    if stream_agent:
        # 测试客户端确认收到当前增量后才继续生成，避免依赖网速或固定等待时间。
        step_reader = asyncio.StreamReader()
        step_transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(step_reader), sys.stdin,
        )

        class StreamChannelEvidence(logging.Handler):
            def emit(self, record):
                request_line = getattr(record, "first_request_line", "")
                status = str(getattr(record, "response_status", ""))
                if "/events" in request_line and status in {"101", "200"}:
                    print(json.dumps({
                        "fixtureEvent": "stream-transport",
                        "transport": "websocket" if status == "101" else "sse",
                    }), file=sys.stderr, flush=True)

        access_log = logging.getLogger("aiohttp.access")
        access_log.handlers = [StreamChannelEvidence()]
        access_log.setLevel(logging.INFO)
        access_log.propagate = False

    async def wait_for_observation():
        if await step_reader.readline() != b"continue\n":
            raise RuntimeError("流式夹具未收到客户端增量观察确认")

    async def stream_reply(event, reply):
        if native_host:
            from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig

            consumer = GatewayStreamConsumer(
                adapter=adapter, chat_id=event.source.chat_id,
                config=StreamConsumerConfig(edit_interval=0.01, buffer_threshold=1, cursor=""),
            )
            task = asyncio.create_task(consumer.run())
            try:
                for fragment in ("fixture-agent-", "reply:\n", event.text):
                    consumer.on_delta(fragment)
                    await wait_for_observation()
                consumer.finish(reply)
                await task
                if not consumer.final_response_sent or consumer.delivered_final_matches(reply) is not True:
                    raise RuntimeError("原生 Hermes 未确认流式回复的最终投递")
            finally:
                if not task.done():
                    task.cancel()
                await task
        else:
            partial = ""
            message_id = None
            for fragment in ("fixture-agent-", "reply:\n", event.text):
                partial += fragment
                result = await adapter.send(event.source.chat_id, partial, metadata={"expect_edits": True}) if message_id is None else await adapter.edit_message(event.source.chat_id, message_id, partial)
                if not result.success:
                    raise RuntimeError("独立夹具未确认流式编辑")
                message_id = result.message_id
                await wait_for_observation()
            if not (await adapter.edit_message(event.source.chat_id, message_id, reply, finalize=True)).success:
                raise RuntimeError("独立夹具未确认流式完成")

    def bound_session_id(event):
        account = core.open_gateway_account("alice")
        try:
            binding = account.agent_sessions.lookup(event.source.chat_id)
            if not binding or not binding.get("agentSessionId"):
                raise RuntimeError("HISTORY_FIXTURE_NATIVE_BINDING_MISSING")
            return binding["agentSessionId"]
        finally:
            account.close()

    async def echo(event):
        # 仅记录夹具执行次数与身份，正文由确定性回复返回，不进入诊断日志。
        print(json.dumps({
            "fixtureEvent": "agent-turn", "conversationId": event.source.chat_id,
            "clientMessageId": event.message_id,
        }), file=sys.stderr, flush=True)
        if session_store is not None:
            session_id = bound_session_id(event)
            session_store.append_to_transcript(session_id, {
                "role": "user", "content": event.text, "message_id": event.message_id,
            })
        reply = ECHO_REPLY_PREFIX + event.text
        # 经生产发送端口保留原样输入；原生宿主随后负责调用完成 hook。
        # 回答已发布时返回 None，避免宿主再次发送或裁剪尾部空白。
        if stream_agent:
            await stream_reply(event, reply)
        else:
            result = await adapter.send(event.source.chat_id, reply)
            if not result.success:
                raise RuntimeError("隔离 Agent 回复未成功回传")
        if session_store is not None:
            session_store.append_to_transcript(session_id, {"role": "assistant", "content": reply})
        if not native_host:
            await adapter.on_processing_complete(event, "success")

    async def history(event):
        # 只替代外部模型推理；接收、原生落库、投递与历史投影均使用生产代码。
        paths = [Path(path) for path in event.media_urls]
        if len(paths) != 1 or not paths[0].read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("HISTORY_FIXTURE_IMAGE_NOT_RECEIVED")
        original_digest = hashlib.sha256(paths[0].read_bytes()).hexdigest()
        session_id = bound_session_id(event)

        # 模型加工文本故意与用户原文不同，锁定图片描述/宿主引用混入历史的故障。
        enriched = "[模型专用合成图片描述]\n[模型专用合成宿主媒体引用]\n\n" + event.text
        session_store.append_to_transcript(session_id, {
            "role": "user", "content": enriched, "message_id": event.message_id,
        })
        progress = await adapter.send(event.source.chat_id, HISTORY_PROGRESS)
        if not progress.success:
            raise RuntimeError("HISTORY_FIXTURE_PROGRESS_NOT_DELIVERED")
        session_store.append_to_transcript(session_id, {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "call_fixture_history", "type": "function",
                "function": {"name": "web_search", "arguments": '{"query":"合成测试图片"}'}}],
        })
        session_store.append_to_transcript(session_id, {
            "role": "tool", "content": '{"synthetic_result":"仅供模型使用的合成返回值"}',
            "tool_name": "web_search", "tool_call_id": "call_fixture_history",
        })
        session_store.append_to_transcript(session_id, {"role": "assistant", "content": HISTORY_REPLY})
        final = await adapter.send(event.source.chat_id, HISTORY_REPLY)
        if not final.success:
            raise RuntimeError("HISTORY_FIXTURE_REPLY_NOT_DELIVERED")
        database = session_store._db_for_session_id(session_id)
        rows = database.get_messages(session_id)
        print(json.dumps({
            "fixtureEvent": "history-persisted", "nativeSessionDb": isinstance(database, SessionDB),
            "imageSha256": original_digest,
            "modelInputPersisted": any(row.get("role") == "user" and row.get("content") == enriched for row in rows),
        }), file=sys.stderr, flush=True)

        # 原生处理任务完成后才记录释放证据，不能在最终回复刚发出时提前宣称清理完成。
        asyncio.current_task().add_done_callback(lambda task: print(json.dumps({
            "fixtureEvent": "history-input-released", "released": all(not path.exists() for path in paths),
        }), file=sys.stderr, flush=True))

    adapter.set_message_handler(history if history_agent else echo)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, stopped.set)
    try:
        if not await adapter.connect():
            raise RuntimeError("隔离适配器未成功启动")
        if not native_host:
            # 旧独立模式只验证 Core/传输边界，未接宿主历史时如实保持未连接。
            # 新 history 模式已在入口拒绝独立回退，不能经此分支逃避原生验证。
            core.history_reader = None
        actual_port = adapter._site._server.sockets[0].getsockname()[1]
        print(f"http://127.0.0.1:{actual_port}", flush=True)
        print(json.dumps({
            "fixtureMode": "history-agent" if history_agent else ("stream-agent" if stream_agent else "echo-agent"),
            "host": "native" if native_host else "standalone",
        }), file=sys.stderr, flush=True)
        await stopped.wait()
    finally:
        await adapter.disconnect()
        if step_transport is not None:
            step_transport.close()
        if native_host:
            for platform_id in platform_ids:
                platform_registry.unregister(platform_id)
        if session_store is not None:
            session_store._db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument(
        "--contract-root", default=None,
        help="Explicit Gateway Protocol contract directory; defaults to the pinned fetch.",
    )
    parser.add_argument(
        "--echo-agent", action="store_true",
        help="使用真实平台 HTTP/SSE 入口和确定性测试 Agent 回传回复。",
    )
    parser.add_argument(
        "--stream-agent", action="store_true",
        help="逐段回传测试回复，标准输入每行 continue 确认一次增量观察。",
    )
    parser.add_argument(
        "--history-agent", action="store_true",
        help="使用指定的真实 Hermes SDK 和 SessionDB，验证原文、工具提示与图片历史。",
    )
    args = parser.parse_args()
    if args.history_agent and (args.echo_agent or args.stream_agent):
        parser.error("--history-agent 不能与 echo/stream 模式同时使用")
    echo_mode = args.echo_agent or args.stream_agent or args.history_agent
    storage = ECHO_STORAGE_ROOT / "gateway" if echo_mode else Path(mkdtemp(prefix="oai-android-interop-"))
    # Exercise the deployed key source when the operator configures one; the
    # in-process test double stays for the plain unit runs.
    key_file = os.environ.get("OAI_GATEWAY_MASTER_KEY_FILE")
    if echo_mode:
        create_master_key_file(key_file)
    secret_store = LocalMasterKeyStore(key_file) if key_file else make_secret_store()
    core = create_gateway_core(storage, secret_store=secret_store, contract_root=contract_root(args.contract_root))
    core.credential_verifier = AccountPasswordVerifier(core)
    compatibility = HostApiCompatibility("1.0.0", "1.0.0", "0123456789abcdef0123456789abcdef01234567")
    admin = create_admin_service(core=core, host_version="1.0.0", host_api=compatibility)
    created = admin.create_account({"accountId": "alice", "password": "android-fixture-only", "localConfirmation": True})
    if not created.get("ok", created.get("success", False)):
        # Preserve the real Admin response for diagnosing fixture setup.
        if "error" in created:
            raise RuntimeError(created)
    exposure = create_gateway_exposure("host-route", core=core, host_version="1.0.0", host_api=compatibility,
                                       verify_request=create_gateway_request_verifier(core))

    if echo_mode:
        asyncio.run(run_echo_agent(core, admin, exposure, args.port,
            stream_agent=args.stream_agent, history_agent=args.history_agent))
        return

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self): self.dispatch()
        def do_POST(self): self.dispatch()
        def do_PUT(self): self.dispatch()
        def do_DELETE(self): self.dispatch()
        # Conversation rename (contract section 7) travels as PATCH; without this
        # handler the fixture would answer 501 and hide a real client defect.
        def do_PATCH(self): self.dispatch()

        def dispatch(self):
            content = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            request = {"method": self.command, "url": self.path, "target": self.path,
                       "headers": dict(self.headers), "rawHeaders": tuple(self.headers.items()), "body": content}
            path = self.path.partition("?")[0]
            route = next((r for r in exposure.routes if
                          (r.match == "exact" and r.path == path) or
                          (r.match == "prefix" and path.startswith(r.path))), None)
            result = route._handle_raw(request) if route else {"statusCode": 404, "body": {"error": {"code": "NOT_FOUND"}}}
            status = result["statusCode"]
            body = result["body"]
            if path.endswith("/events") and status == 200 and "text/event-stream" in self.headers.get("Accept", ""):
                encoded = b": fixture connected\n\n"
                mime = "text/event-stream"
            else:
                encoded = json.dumps(body, ensure_ascii=False).encode()
                mime = "application/json"
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            print(json.dumps({"method": self.command, "path": path, "status": status,
                              "error": body.get("error", {}).get("code")}), file=sys.stderr, flush=True)

        def log_message(self, *_): pass

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"http://127.0.0.1:{server.server_port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
