"""原生工具必须在设备离线或没有回传时及时结束，并保留请求关联。"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from open_android_intelligence_gateway import device_tools
from open_android_intelligence_gateway.core import create_gateway_core, GatewayError
from test_review_regressions import register
from test_support import make_secret_store, seed_event_session


@pytest.mark.parametrize("claimed", [False, True])
def test_native_tool_has_an_independent_short_wait_and_fences_abandoned_request(tmp_path, monkeypatch, claimed):
    core = create_gateway_core(tmp_path, secret_store=make_secret_store())
    seed_event_session(core)
    account = core.open_gateway_account("acct_alice")
    try:
        binding = register(account, {"type": "object", "additionalProperties": False})
    finally:
        account.close()
    monkeypatch.setattr(device_tools, "DEVICE_TOOL_WAIT_SECONDS", .03, raising=False)

    async def scenario():
        token = device_tools.trusted_turn.set({
            "core": core, "accountId": "acct_alice", "deviceId": "dev_1",
            "pairingGeneration": 1, "grantRevision": 1, "conversationId": "conv_native",
            "messageId": "msg_native", "sessionId": "native_one",
        })
        try:
            task = asyncio.create_task(device_tools.execute(core, {
                "capabilityId": binding["capabilityId"], "capabilityVersion": "2.0.0", "parameters": {},
            }, session_id="native_one"))
            await asyncio.sleep(0)
            account = core.open_gateway_account("acct_alice")
            try:
                request_id = account.store.database.execute("SELECT request_id FROM device_requests").fetchone()[0]
                if claimed:
                    claim = account.device_requests.claim(request_id, "dev_1", 1, 1, "cor_claim")
                result = await asyncio.wait_for(task, .5)
                assert result["requestId"] == request_id
                assert result["outcome"] == ("outcome_unknown" if claimed else "failed")
                assert result["code"] == ("DEVICE_TIMEOUT" if claimed else "DEVICE_OFFLINE")
                assert result["stage"] == ("result_wait" if claimed else "claim_wait")
                assert account.device_requests.get(request_id)["state"] == ("cancel_requested" if claimed else "cancelled")
                if claimed:
                    account.device_requests.submit_result(request_id, "dev_1", 1, 1, claim["claimId"],
                        {"outcome": "succeeded", "data": {"actual": True}}, "cor_result")
                    assert account.device_requests.read_result(request_id, claim["claimId"])["data"] == {"actual": True}
                else:
                    with pytest.raises(GatewayError, match="OUTCOME_UNKNOWN"):
                        account.device_requests.claim(request_id, "dev_1", 1, 1, "cor_late")
            finally:
                account.close()
        finally:
            device_tools.trusted_turn.reset(token)
    asyncio.run(scenario())


@pytest.fixture
def registered(tmp_path):
    core = create_gateway_core(tmp_path, secret_store=make_secret_store())
    seed_event_session(core)
    account = core.open_gateway_account("acct_alice")
    try:
        binding = register(account, {"type": "object", "additionalProperties": False})
    finally:
        account.close()
    return core, binding


async def start_tool(core, binding):
    token = device_tools.trusted_turn.set({
        "core": core, "accountId": "acct_alice", "deviceId": "dev_1",
        "pairingGeneration": 1, "grantRevision": 1, "conversationId": "conv_native",
        "messageId": "msg_native", "sessionId": "native_one",
    })
    try:
        task = asyncio.create_task(device_tools.execute(core, {
            "capabilityId": binding["capabilityId"], "capabilityVersion": "2.0.0", "parameters": {},
        }, session_id="native_one"))
    finally:
        device_tools.trusted_turn.reset(token)
    await asyncio.sleep(0)
    account = core.open_gateway_account("acct_alice")
    try:
        request_id = account.store.database.execute("SELECT request_id FROM device_requests ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    finally:
        account.close()
    return task, request_id


def test_claimed_timeout_has_bounded_durable_recovery_after_host_restarts(registered, monkeypatch):
    from datetime import timedelta
    from open_android_intelligence_gateway.core import _now
    core, binding = registered
    monkeypatch.setattr(device_tools, "DEVICE_TOOL_WAIT_SECONDS", .03)

    async def scenario():
        task, request_id = await start_tool(core, binding)
        account = core.open_gateway_account("acct_alice")
        try:
            claim = account.device_requests.claim(request_id, "dev_1", 1, 1, "cor_claim")
            record = account.device_requests.get(request_id)
            assert (_now(record["expiresAt"]) - _now(record["createdAt"])).total_seconds() == 90
        finally:
            account.close()
        assert (await asyncio.wait_for(task, .5))["outcome"] == "outcome_unknown"
        reopened = core.open_gateway_account("acct_alice")
        try:
            expiry = _now(record["expiresAt"]) + timedelta(milliseconds=1)
            assert reopened.device_requests.recover_expired(expiry) == 1
            assert reopened.device_requests.get(request_id)["state"] == "outcome_unknown"
            assert reopened.device_requests.get(request_id)["parameters"] == {}
            with pytest.raises(GatewayError, match="OUTCOME_UNKNOWN"):
                reopened.device_requests.submit_result(request_id, "dev_1", 1, 1, claim["claimId"],
                    {"outcome": "succeeded", "data": {}}, "cor_late", expiry)
        finally:
            reopened.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("claimed", [False, True])
@pytest.mark.parametrize("revocation", ["grant", "capability"])
def test_revocation_returns_correlated_error_and_preserves_execution_uncertainty(registered, claimed, revocation):
    from open_android_intelligence_gateway.capability_policy import DeviceCapabilityPolicy
    core, binding = registered

    async def scenario():
        task, request_id = await start_tool(core, binding)
        account = core.open_gateway_account("acct_alice")
        try:
            if claimed:
                account.device_requests.claim(request_id, "dev_1", 1, 1, "cor_claim")
            if revocation == "grant":
                account.store.database.execute("UPDATE device_keys SET grant_revision=2 WHERE device_id='dev_1'")
            else:
                DeviceCapabilityPolicy(account.store).set(binding["capabilityId"], False)
            with pytest.raises(GatewayError) as rejected:
                await asyncio.wait_for(task, .5)
            assert rejected.value.code == ("GRANT_STALE" if revocation == "grant" else "CAPABILITY_DENIED")
            assert rejected.value.details["requestId"] == request_id
            assert rejected.value.details["stage"] == ("result_wait" if claimed else "claim_wait")
            assert rejected.value.details["outcome"] == ("outcome_unknown" if claimed else "denied")
            assert account.device_requests.get(request_id)["state"] == ("cancel_requested" if claimed else "cancelled")
        finally:
            account.close()
    asyncio.run(scenario())


def test_expiry_scan_does_not_overwrite_a_result_committed_after_candidate_selection(registered, monkeypatch):
    from contextlib import contextmanager
    from datetime import timedelta
    from open_android_intelligence_gateway.core import _now
    core, binding = registered
    account = core.open_gateway_account("acct_alice")
    try:
        request = account.device_requests.enqueue("devreq_expiry_race", "dev_1", 1, 1, "read",
            {"id": binding["capabilityId"], "version": "2.0.0"},
            {"pluginId": binding["pluginId"], "authorKeyId": binding["authorKeyId"]}, {}, "cor_enqueue",
            timeout_seconds=90)
        claim = account.device_requests.claim(request["requestId"], "dev_1", 1, 1, "cor_claim")
        transaction = account.store.transaction
        submitted = False

        @contextmanager
        def race_transaction(*args, **kwargs):
            nonlocal submitted
            if not submitted:
                submitted = True
                account.device_requests.submit_result(request["requestId"], "dev_1", 1, 1, claim["claimId"],
                    {"outcome": "succeeded", "data": {"actual": True}}, "cor_result",
                    _now(request["expiresAt"]) - timedelta(milliseconds=1))
            with transaction(*args, **kwargs) as connection:
                yield connection

        monkeypatch.setattr(account.store, "transaction", race_transaction)
        assert account.device_requests.recover_expired(_now(request["expiresAt"])) == 0
        assert account.device_requests.get(request["requestId"])["state"] == "succeeded"
    finally:
        account.close()


def test_internal_failure_is_correlated_and_diagnostics_never_log_private_exception_body(registered, monkeypatch, caplog):
    from open_android_intelligence_gateway.core import DeviceRequestStore
    core, binding = registered

    async def scenario():
        task, request_id = await start_tool(core, binding)
        original = DeviceRequestStore._assert_capability_enabled
        injected = False

        def fail_once(store, row):
            nonlocal injected
            if not injected:
                injected = True
                raise RuntimeError("private-notification-body secret-token")
            return original(store, row)

        monkeypatch.setattr(DeviceRequestStore, "_assert_capability_enabled", fail_once)
        with pytest.raises(GatewayError, match="INTERNAL_ERROR") as failed:
            await asyncio.wait_for(task, .5)
        assert failed.value.details["requestId"] == request_id
        assert "private-notification-body" not in str(failed.value.details)
        assert request_id in caplog.text and "RuntimeError" in caplog.text
        assert "private-notification-body" not in caplog.text and "secret-token" not in caplog.text
        account = core.open_gateway_account("acct_alice")
        try:
            assert account.device_requests.get(request_id)["state"] == "cancelled"
        finally:
            account.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("risk,online,expected", [("read", False, 86400), ("write", True, 900), ("high-privilege-ephemeral", True, 30), ("high-privilege-ephemeral", False, 0)])
def test_optional_native_timeout_never_extends_existing_risk_limits(tmp_path, risk, online, expected):
    from open_android_intelligence_gateway.core import _now
    core = create_gateway_core(tmp_path, secret_store=make_secret_store())
    seed_event_session(core)
    account = core.open_gateway_account("acct_alice")
    try:
        binding = register(account, {"type": "object", "additionalProperties": False}, risk=risk)
        args = ("dev_1", 1, 1, risk, {"id": binding["capabilityId"], "version": "2.0.0"},
            {"pluginId": binding["pluginId"], "authorKeyId": binding["authorKeyId"]}, {}, "cor_enqueue")
        for request_id, timeout, lifetime in [("default", None, expected), ("native", 90, min(90, expected))]:
            request = account.device_requests.enqueue(request_id, *args, timeout_seconds=timeout, online=online)
            assert (_now(request["expiresAt"]) - _now(request["createdAt"])).total_seconds() == lifetime
    finally:
        account.close()


def test_connected_event_stream_without_claim_is_still_an_explicit_claim_timeout(registered, monkeypatch):
    from open_android_intelligence_gateway.core import VerifiedRequestContext
    core, binding = registered
    context = VerifiedRequestContext(account_id="acct_alice", device_id="dev_1", session_id="sess_1",
        request_id="req_online", correlation_id="cor_online", pairing_generation=1, grant_revision=1)
    core.set_device_online(context, True)
    monkeypatch.setattr(device_tools, "DEVICE_TOOL_WAIT_SECONDS", .03)

    async def scenario():
        task, request_id = await start_tool(core, binding)
        result = await asyncio.wait_for(task, .5)
        assert result == {"requestId": request_id, "outcome": "failed", "code": "DEVICE_TIMEOUT", "stage": "claim_wait"}
    asyncio.run(scenario())


def test_external_cancellation_is_not_reported_as_network_failure(registered):
    core, binding = registered

    async def scenario():
        task, request_id = await start_tool(core, binding)
        account = core.open_gateway_account("acct_alice")
        try:
            account.device_requests.cancel(request_id, "dev_1", 1, 1, "cor_cancel")
        finally:
            account.close()
        result = await asyncio.wait_for(task, .5)
        assert result == {"requestId": request_id, "outcome": "cancelled", "code": "DEVICE_CANCELLED", "stage": "claim_wait"}
    asyncio.run(scenario())


def test_committed_success_wins_when_tool_wait_budget_is_already_exhausted(registered, monkeypatch):
    from open_android_intelligence_gateway.core import DeviceRequestStore
    core, binding = registered
    enqueue = DeviceRequestStore.enqueue

    def complete_before_first_poll(store, *args, **kwargs):
        request = enqueue(store, *args, **kwargs)
        claim = store.claim(request["requestId"], "dev_1", 1, 1, "cor_claim")
        store.submit_result(request["requestId"], "dev_1", 1, 1, claim["claimId"],
            {"outcome": "succeeded", "data": {"actual": True}}, "cor_result")
        return request

    monkeypatch.setattr(DeviceRequestStore, "enqueue", complete_before_first_poll)
    monkeypatch.setattr(device_tools, "DEVICE_TOOL_WAIT_SECONDS", 0)

    async def scenario():
        task, request_id = await start_tool(core, binding)
        assert await asyncio.wait_for(task, .5) == {
            "requestId": request_id, "outcome": "succeeded", "data": {"actual": True},
        }
        account = core.open_gateway_account("acct_alice")
        try:
            assert account.device_requests.get(request_id)["state"] == "succeeded"
        finally:
            account.close()
    asyncio.run(scenario())
