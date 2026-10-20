"""Account policy -> authenticated publication -> Agent -> device claim/result regression."""
import asyncio
import hashlib
import uuid

import pytest

from open_android_intelligence_gateway import device_tools
from open_android_intelligence_gateway.admin import create_admin_service, run_admin_command, VERIFIED_HERMES_HOST_API
from open_android_intelligence_gateway.capability_policy import DeviceCapabilityPolicy, capability_catalog
from open_android_intelligence_gateway.core import create_gateway_core, GatewayError, VerifiedRequestContext, _jcs
from open_android_intelligence_gateway.plugin import interactive_setup
from test_support import make_secret_store, seed_event_session, make_verified_request

NOTIFICATIONS = "org.openandroidintelligence.notifications.query"


def binding(capability_id=NOTIFICATIONS):
    schema = {"type": "object", "additionalProperties": False, "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}}}
    return {"pluginId": capability_id.rsplit(".", 1)[0], "authorKeyId": "sha256:" + "a" * 64,
            "capabilityId": capability_id, "capabilityVersion": "1.0.0", "risk": "read",
            "schema": schema, "schemaSha256": "sha256:" + hashlib.sha256(_jcs(schema).encode()).hexdigest()}


def state(item, status="READY", reason=""):
    return {k: item[k] for k in ("pluginId", "capabilityId", "capabilityVersion")} | {"status": status, "reason": reason}


@pytest.fixture
def gateway(tmp_path):
    secrets = make_secret_store()
    core = create_gateway_core(tmp_path, secret_store=secrets)
    seed_event_session(core)
    admin = create_admin_service(core=core, host_version="0.20.0", host_api=VERIFIED_HERMES_HOST_API)
    return core, admin, secrets


def call(core, method="GET", path="pairings/current", body=None):
    account = core.open_gateway_account("acct_alice")
    try:
        generation, revision = account.store.database.execute("SELECT pairing_generation,grant_revision FROM device_keys WHERE device_id='dev_1'").fetchone()
    finally:
        account.close()
    request_id = "req_" + uuid.uuid4().hex
    context = VerifiedRequestContext(account_id="acct_alice", device_id="dev_1", session_id="sess_1",
        request_id=request_id, correlation_id=request_id, pairing_generation=generation, grant_revision=revision)
    return core.handle(make_verified_request({"method": method, "target": "/open-android-intelligence/v2/" + path,
        "body": body, "context": context, "idempotencyKey": request_id if method != "GET" else None}))


def publish(core, bindings, inventory=None, local=1):
    current = call(core)["data"]
    body = {"bindings": bindings, "localGrantRevision": local, "expectedGrantRevision": current["grantRevision"]}
    if inventory is not None:
        body["inventory"] = inventory
    return call(core, "POST", "pairings/current/capabilities", body)


def set_switch(admin, enabled, capability=NOTIFICATIONS, account="acct_alice"):
    return run_admin_command(["capabilities", "set", account, capability, "on" if enabled else "off", "--confirm-local"], admin)


def origin(core, revision):
    return {"core": core, "accountId": "acct_alice", "deviceId": "dev_1", "pairingGeneration": 1,
            "grantRevision": revision, "conversationId": "conv_test", "messageId": "msg_test", "sessionId": "native"}


def test_switch_is_account_local_durable_default_deny_and_never_creates_android_grants(gateway, tmp_path):
    core, admin, secrets = gateway
    seed_event_session(core, "acct_bob", "dev_bob", "sess_bob")
    assert set_switch(admin, True)["ok"]
    first = call(core)["data"]
    assert first["grantRevision"] == 1  # Gateway policy is a separate revision.
    assert next(item for item in first["capabilities"] if item["capabilityId"] == NOTIFICATIONS)["androidStatus"] == "SYNC_REQUIRED"
    reloaded = create_gateway_core(tmp_path, secret_store=secrets)
    for account_id, expected in [("acct_alice", True), ("acct_bob", False)]:
        account = reloaded.open_gateway_account(account_id)
        try:
            assert DeviceCapabilityPolicy(account.store).allows(NOTIFICATIONS) is expected
            assert account.device_requests.capabilities.available("dev_1", 1, 1) == []
        finally:
            account.close()


@pytest.mark.parametrize("android_status,reason", [
    ("PLUGIN_DISABLED", "PLUGIN_DISABLED"), ("LOCAL_GRANT_REQUIRED", "PAIRING_GRANT_REQUIRED"),
    ("ANDROID_PERMISSION_REQUIRED", "NOTIFICATION_ACCESS_REQUIRED"),
    ("LOCAL_GRANT_REQUIRED", "LOCAL_GRANT_REQUIRED"), ("PROVIDER_UNAVAILABLE", "NOTIFICATION_LISTENER_UNAVAILABLE"),
])
def test_android_gate_diagnostics_do_not_become_bindings(gateway, android_status, reason):
    core, admin, _ = gateway
    assert set_switch(admin, True)["ok"]
    assert "data" in publish(core, [], [state(binding(), android_status, reason)])
    current = call(core)["data"]
    item = next(item for item in current["capabilities"] if item["capabilityId"] == NOTIFICATIONS)
    assert item["status"] == android_status
    assert item["reason"] == reason
    token = device_tools.trusted_turn.set(origin(core, current["grantRevision"]))
    try:
        with pytest.raises(GatewayError) as denied:
            asyncio.run(device_tools.execute(core, {"capabilityId": NOTIFICATIONS, "capabilityVersion": "1.0.0", "parameters": {}}, session_id="native"))
        assert denied.value.details["reason"] == android_status
        assert device_tools.discover(core, {}, session_id="native")["capabilities"]
        with pytest.raises(GatewayError, match="SCHEMA_INVALID"):
            device_tools.discover(core, {"deviceId": "dev_bob"}, session_id="native")
    finally:
        device_tools.trusted_turn.reset(token)


def test_empty_inventory_means_not_installed_legacy_empty_means_unknown_and_stale_means_sync_required(gateway):
    core, admin, _ = gateway
    set_switch(admin, True)
    publish(core, [])
    def status():
        return next(item for item in call(core)["data"]["capabilities"] if item["capabilityId"] == NOTIFICATIONS)["androidStatus"]
    assert status() == "ANDROID_STATE_UNKNOWN"
    publish(core, [], [])
    assert status() == "PLUGIN_NOT_INSTALLED"
    account = core.open_gateway_account("acct_alice")
    try:
        account.bump_grant_revision("dev_1", "local-revoke")
    finally:
        account.close()
    assert status() == "SYNC_REQUIRED"
    assert "grantDigest" not in call(core)["data"]


def test_repairing_never_acknowledges_an_old_generations_identical_digest(gateway):
    core, _, _ = gateway
    item = binding()
    original = publish(core,[item],[state(item)])["data"]
    account = core.open_gateway_account("acct_alice")
    try:
        # Reproduce a re-pair with the reported generation/revision and retained old metadata.
        account.store.database.execute("UPDATE device_keys SET pairing_generation=4,grant_revision=2 WHERE device_id='dev_1'")
    finally:
        account.close()
    assert "grantDigest" not in call(core)["data"]
    restored = publish(core,[item],[state(item)])["data"]
    assert restored["grantDigest"] == original["grantDigest"]
    assert restored["grantRevision"] == 3
    account = core.open_gateway_account("acct_alice")
    try:
        assert account.device_requests.capabilities.list("dev_1",4,3) == [item]
    finally:
        account.close()


def test_dynamic_setup_and_cli_support_declared_capabilities_and_existing_accounts(gateway, capsys):
    core, admin, _ = gateway
    custom = binding("org.example.weather.fetch")
    publish(core, [], [state(custom, "LOCAL_GRANT_REQUIRED", "PAIRING_GRANT_REQUIRED")])
    answers = iter(["acct_alice", "y", "n", "y", "n"])
    assert interactive_setup(admin, input_fn=lambda _: next(answers), is_tty=True)
    result = run_admin_command(["capabilities", "list", "acct_alice"], admin)
    assert result["data"]["policy"]["enabled"][custom["capabilityId"]] is True
    assert result["data"]["policy"]["enabled"][NOTIFICATIONS] is True
    assert "org.example.weather.fetch" in capsys.readouterr().out
    assert run_admin_command(["capabilities", "set", "acct_alice", NOTIFICATIONS, "off"], admin)["error"]["code"] == "LOCAL_CONFIRMATION_REQUIRED"
    assert run_admin_command(["capabilities", "set", "acct_alice", NOTIFICATIONS, "bogus"], admin)["ok"] is False
    assert run_admin_command(["capabilities", "list", "missing"], admin)["error"]["code"] == "ACCOUNT_NOT_FOUND"


def test_publication_replay_restart_and_notification_result_roundtrip(gateway, tmp_path):
    core, admin, secrets = gateway
    item = binding()
    published = publish(core, [item], [state(item)])
    revision = published["data"]["grantRevision"]
    assert publish(core, [item], [state(item)])["data"]["grantRevision"] == revision
    assert set_switch(admin, True)["ok"]
    core = create_gateway_core(tmp_path, secret_store=secrets)
    token = device_tools.trusted_turn.set(origin(core, revision))
    async def scenario():
        request = asyncio.create_task(device_tools.execute(core, {"capabilityId": NOTIFICATIONS, "capabilityVersion": "1.0.0", "parameters": {"limit": 1}}, session_id="native"))
        await asyncio.sleep(0)
        account = core.open_gateway_account("acct_alice")
        try:
            request_id = account.store.database.execute("SELECT request_id FROM device_requests").fetchone()[0]
            claim = account.device_requests.claim(request_id, "dev_1", 1, revision, "claim")
            account.device_requests.submit_result(request_id, "dev_1", 1, revision, claim["claimId"],
                {"outcome": "succeeded", "data": {"records": [{"packageName": "com.mail", "text": "notification"}]}}, "result")
        finally:
            account.close()
        result = await asyncio.wait_for(request, 2)
        assert result["data"]["records"][0]["text"] == "notification"
    try:
        asyncio.run(scenario())
    finally:
        device_tools.trusted_turn.reset(token)


@pytest.mark.parametrize("claimed", [False, True])
def test_disabling_fences_queued_claimed_and_late_results_even_after_reenable(gateway, claimed):
    core, admin, _ = gateway
    item = binding()
    revision = publish(core, [item], [state(item)])["data"]["grantRevision"]
    set_switch(admin, True)
    account = core.open_gateway_account("acct_alice")
    try:
        account.device_requests.enqueue("request_disable", "dev_1", 1, revision, "read",
            {"id": NOTIFICATIONS, "version": "1.0.0"}, {k: item[k] for k in ("pluginId", "authorKeyId")}, {}, "queue")
        claim = account.device_requests.claim("request_disable", "dev_1", 1, revision, "claim") if claimed else None
    finally:
        account.close()
    assert set_switch(admin, False)["ok"]
    assert call(core, path="device-requests/request_disable")["error"]["code"] == "CAPABILITY_DENIED"
    assert set_switch(admin, True)["ok"]
    account = core.open_gateway_account("acct_alice")
    try:
        assert account.device_requests.get("request_disable")["state"] == ("cancel_requested" if claimed else "cancelled")
        with pytest.raises(GatewayError, match="CAPABILITY_DENIED"):
            account.device_requests.claim("request_disable", "dev_1", 1, revision, "claim_again")
        if claim:
            with pytest.raises(GatewayError, match="CAPABILITY_DENIED"):
                account.device_requests.submit_result("request_disable", "dev_1", 1, revision, claim["claimId"], {"outcome": "succeeded", "data": {"secret": "late"}}, "late")
    finally:
        account.close()


def test_old_generation_or_wrong_schema_never_dispatches_and_corrupt_policy_denies(gateway):
    core, admin, _ = gateway
    item = binding()
    set_switch(admin, True)
    publish(core, [item], [state(item)])
    account = core.open_gateway_account("acct_alice")
    try:
        assert not account.device_requests.capabilities.available("dev_1", 2, 1)
        assert account.device_requests.capabilities.available("dev_1", 1, 2)
        with pytest.raises(GatewayError, match="SCHEMA_INVALID"):
            account.device_requests.enqueue("request_bad", "dev_1", 1, 2, "read", {"id": NOTIFICATIONS, "version": "1.0.0"},
                {k: item[k] for k in ("pluginId", "authorKeyId")}, {"limit": 101}, "bad")
        account.store.database.execute("UPDATE account_metadata SET value='invalid' WHERE key='device-capability-policy:v1'")
        with pytest.raises(GatewayError) as error:
            account.device_requests.capabilities.available("dev_1", 1, 2)
        assert error.value.details["reason"] == "CAPABILITY_POLICY_INVALID"
    finally:
        account.close()


def test_native_hermes_argparse_cli_uses_the_same_account_policy(gateway, tmp_path, capsys):
    from test_plugin_surface import CliHostContext, _run_contract_cli
    from open_android_intelligence_gateway.plugin import register
    core, _, _ = gateway
    ctx = CliHostContext(tmp_path, gateway_core=core, host_version="0.20.0", host_api=VERIFIED_HERMES_HOST_API)
    register(ctx)
    report = _run_contract_cli(ctx, capsys, "capabilities", "set", "acct_alice", NOTIFICATIONS, "on", "--confirm-local")
    assert '"ok": true' in report
    report = _run_contract_cli(ctx, capsys, "capabilities", "list", "acct_alice")
    assert NOTIFICATIONS in report and '"gatewayEnabled": true' in report


def test_agent_tools_register_discovery_and_return_actionable_denial(gateway):
    core, _, _ = gateway
    class Context:
        def __init__(self): self.tools = {}
        def register_tool(self, **kwargs): self.tools[kwargs["name"]] = kwargs
        def register_hook(self, *_): pass
    context = Context()
    device_tools.register_device_tools(context, core)
    assert set(context.tools) == {"open_android_device", "open_android_capabilities"}
    token = device_tools.trusted_turn.set(origin(core, 1))
    try:
        response = asyncio.run(context.tools["open_android_device"]["handler"](
            {"capabilityId": NOTIFICATIONS, "capabilityVersion": "1.0.0", "parameters": {}}, session_id="native"))
        assert '"reason":"GATEWAY_DISABLED"' in response
        assert '"code":"CAPABILITY_DENIED"' in response
    finally:
        device_tools.trusted_turn.reset(token)


def test_android_grant_change_during_a_tool_call_fences_result_delivery(gateway):
    core, admin, _ = gateway
    item = binding()
    revision = publish(core,[item],[state(item)])["data"]["grantRevision"]
    set_switch(admin,True)
    async def scenario():
        token = device_tools.trusted_turn.set(origin(core,revision))
        try:
            task = asyncio.create_task(device_tools.execute(core,{"capabilityId":NOTIFICATIONS,"capabilityVersion":"1.0.0","parameters":{}},session_id="native"))
            await asyncio.sleep(0)
            account = core.open_gateway_account("acct_alice")
            try:
                account.bump_grant_revision("dev_1","revoke")
            finally:
                account.close()
            with pytest.raises(GatewayError,match="GRANT_STALE"):
                await asyncio.wait_for(task,2)
        finally:
            device_tools.trusted_turn.reset(token)
    asyncio.run(scenario())


@pytest.mark.parametrize("inventory", [None, [{"status": "READY"}], [state(binding(), "BOGUS")], [state(binding())]*129, [state(binding())]])
def test_invalid_or_inconsistent_inventory_cannot_change_a_grant(gateway, inventory):
    core, _, _ = gateway
    before = call(core)["data"]
    body = {"bindings": [], "inventory": inventory, "expectedGrantRevision": 1, "localGrantRevision": 1}
    assert call(core, "POST", "pairings/current/capabilities", body)["error"]["code"] == "SCHEMA_INVALID"
    assert call(core)["data"] == before
