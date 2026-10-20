"""Isolated real HTTP/signature boundary for Android capability end-to-end tests.

stdin/stdout is a test-only local host control channel, never an HTTP admin route.
Android must log in, attest capabilities and claim/result using production signatures.
"""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Thread

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from open_android_intelligence_gateway.adapter import AccountPasswordVerifier, create_gateway_request_verifier
from open_android_intelligence_gateway.admin import create_admin_service, HostApiCompatibility
from open_android_intelligence_gateway.core import create_gateway_core, GatewayError, VerifiedRequestContext
from open_android_intelligence_gateway.device_tools import execute, trusted_turn
from open_android_intelligence_gateway.http import create_gateway_exposure
from open_android_intelligence_gateway.local_keys import create_master_key_file, LocalMasterKeyStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract-root", required=True)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="oai-device-e2e-") as directory:
        root = Path(directory)
        key = create_master_key_file(root / "key" / "master")
        core = create_gateway_core(root / "gateway", secret_store=LocalMasterKeyStore(key), contract_root=args.contract_root)
        core.credential_verifier = AccountPasswordVerifier(core)
        compatibility = HostApiCompatibility("1.0.0", "1.0.0", "0" * 40)
        admin = create_admin_service(core=core, host_version="1.0.0", host_api=compatibility)
        assert admin.create_account({"accountId": "alice", "password": "device-fixture-only", "localConfirmation": True})["ok"]
        exposure = create_gateway_exposure("host-route", core=core, host_version="1.0.0", host_api=compatibility,
                                           verify_request=create_gateway_request_verifier(core))

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self): self.dispatch()
            def do_POST(self): self.dispatch()

            def dispatch(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                raw = {"method": self.command, "url": self.path, "target": self.path, "headers": dict(self.headers),
                       "rawHeaders": tuple(self.headers.items()), "body": body}
                path = self.path.partition("?")[0]
                route = next((route for route in exposure.routes if
                    (route.match == "exact" and route.path == path) or (route.match == "prefix" and path.startswith(route.path))), None)
                result = route._handle_raw(raw) if route else {"statusCode": 404, "body": {}}
                content = json.dumps(result["body"]).encode()
                self.send_response(result["statusCode"])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, *_): pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        Thread(target=server.serve_forever, daemon=True).start()
        print(f"http://127.0.0.1:{server.server_port}", flush=True)
        pool = ThreadPoolExecutor(max_workers=1)
        task = None

        def invoke(command):
            account = core.open_gateway_account("alice")
            try:
                row = account.store.database.execute("SELECT device_id,pairing_generation,grant_revision FROM device_keys").fetchone()
                origin = {"core": core, "accountId": "alice", "deviceId": row[0], "pairingGeneration": row[1],
                          "grantRevision": row[2], "conversationId": "conv_fixture", "messageId": "msg_fixture", "sessionId": "native_fixture"}
                session_id = account.store.database.execute("SELECT session_id FROM access_sessions WHERE device_id=? ORDER BY rowid DESC LIMIT 1", (row[0],)).fetchone()[0]
                core.set_device_online(VerifiedRequestContext(account_id="alice",device_id=row[0],session_id=session_id,
                    request_id="fixture_presence",correlation_id="fixture_presence",pairing_generation=row[1],grant_revision=row[2]),True)
            finally:
                account.close()
            async def turn():
                token = trusted_turn.set(origin)
                try:
                    return await execute(core, {"capabilityId": command["capabilityId"], "capabilityVersion": "1.0.0",
                                               "parameters": command.get("parameters", {})}, session_id="native_fixture")
                except GatewayError as error:
                    return {"code": error.code, **error.details}
                finally:
                    trusted_turn.reset(token)
            return asyncio.run(turn())

        try:
            for line in sys.stdin:
                command = json.loads(line)
                operation = command["operation"]
                if operation == "switch":
                    result = admin.execute({"command": "capabilities.set", "accountId": "alice", "capabilityId": command["capabilityId"],
                                            "enabled": command["enabled"], "localConfirmation": True})
                elif operation == "start":
                    assert task is None or task.done()
                    task = pool.submit(invoke, command)
                    result = {"started": True}
                elif operation == "pending":
                    account = core.open_gateway_account("alice")
                    try:
                        row = account.store.database.execute("SELECT request_id FROM device_requests WHERE state='pending' ORDER BY rowid DESC LIMIT 1").fetchone()
                        result = {"requestId": row[0] if row else None}
                    finally:
                        account.close()
                elif operation == "result":
                    result = task.result(timeout=10)
                else:
                    raise ValueError("Unknown fixture operation")
                print(json.dumps(result), flush=True)
        finally:
            server.shutdown()
            pool.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
