"""Android HTTP 互通夹具，账号和服务均与用户 Gateway 隔离。

默认模式沿用 Core、HTTP 路由、Schema、密码存储与 Ed25519 验签；
echo 模式额外运行生产平台适配器、真实临时 AES-GCM 密钥与确定性测试 Agent。
"""
from __future__ import annotations

import argparse
import asyncio
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
# 默认夹具分支保留原来的环境行为，只有显式 echo 模式强制隔离。
ECHO_STORAGE_ROOT = Path(mkdtemp(prefix="oai-android-echo-")) if any(
    flag in sys.argv for flag in ("--echo-agent", "--stream-agent")
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


async def run_echo_agent(core, admin, exposure, port, *, stream_agent=False):
    """测试专用确定性 Agent，经生产适配器完成消息回合与 SSE 回复。"""
    from open_android_intelligence_gateway.adapter import BasePlatformAdapter, OpenAndroidPlatformAdapter
    from open_android_intelligence_gateway.platform_identity import GATEWAY_PLATFORM_ID
    from open_android_intelligence_gateway.plugin import GatewayServices

    native_host = BasePlatformAdapter.__module__ == "gateway.platforms.base"
    if os.environ.get("OAI_REQUIRE_NATIVE_HERMES") == "1" and not native_host:
        raise RuntimeError("本次互通必须加载真实 Hermes BasePlatformAdapter")
    if native_host:
        from gateway.config import PlatformConfig
        from gateway.platform_registry import PlatformEntry, platform_registry

        platform_registry.register(PlatformEntry(
            name=GATEWAY_PLATFORM_ID, label="Android 隔离互通夹具",
            adapter_factory=OpenAndroidPlatformAdapter, check_fn=lambda: True,
        ))
        config = PlatformConfig(extra={"host": "127.0.0.1", "account_id": "alice"})
    else:
        config = SimpleNamespace(extra={"host": "127.0.0.1", "account_id": "alice"})
    adapter = OpenAndroidPlatformAdapter(config, GatewayServices(core, admin, exposure))
    adapter._port = port
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

    async def echo(event):
        # 仅记录夹具执行次数与身份，正文由确定性回复返回，不进入诊断日志。
        print(json.dumps({
            "fixtureEvent": "agent-turn", "conversationId": event.source.chat_id,
            "clientMessageId": event.message_id,
        }), file=sys.stderr, flush=True)
        reply = ECHO_REPLY_PREFIX + event.text
        # 经生产发送端口保留原样输入；原生宿主随后负责调用完成 hook。
        # 回答已发布时返回 None，避免宿主再次发送或裁剪尾部空白。
        if stream_agent:
            await stream_reply(event, reply)
        else:
            result = await adapter.send(event.source.chat_id, reply)
            if not result.success:
                raise RuntimeError("隔离 Agent 回复未成功回传")
        if not native_host:
            await adapter.on_processing_complete(event, "success")

    adapter.set_message_handler(echo)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, stopped.set)
    try:
        if not await adapter.connect():
            raise RuntimeError("隔离适配器未成功启动")
        actual_port = adapter._site._server.sockets[0].getsockname()[1]
        print(f"http://127.0.0.1:{actual_port}", flush=True)
        print(json.dumps({
            "fixtureMode": "stream-agent" if stream_agent else "echo-agent", "host": "native" if native_host else "standalone",
        }), file=sys.stderr, flush=True)
        await stopped.wait()
    finally:
        await adapter.disconnect()
        if step_transport is not None:
            step_transport.close()
        if native_host:
            platform_registry.unregister(GATEWAY_PLATFORM_ID)


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
    args = parser.parse_args()
    echo_mode = args.echo_agent or args.stream_agent
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
        asyncio.run(run_echo_agent(core, admin, exposure, args.port, stream_agent=args.stream_agent))
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
