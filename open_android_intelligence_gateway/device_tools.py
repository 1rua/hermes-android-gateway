"""Native Hermes tools: all routing facts come from the running host session."""
from __future__ import annotations
import asyncio
from contextvars import ContextVar
import hashlib
import json
import logging
import threading
import uuid
from typing import Any, Mapping

TOOL_NAME = "open_android_device"
DEVICE_TOOL_WAIT_SECONDS = 60.0
# 工具结束后仍给设备 30 秒回传宽限；请求失效后禁止再次执行。
DEVICE_REQUEST_TIMEOUT_SECONDS = 90.0
logger = logging.getLogger(__name__)
trusted_turn: ContextVar[dict[str, Any] | None] = ContextVar("oai_trusted_device_turn",default=None)
_pending: dict[tuple[int,str,str], tuple[str,str,str]] = {}
_pending_lock = threading.RLock()

# Keep routing and recovery instructions in the registered tool descriptions:
# the native host exposes these to the Agent even without a separate skill.
DEVICE_ROUTING_GUIDANCE = (
    "For the phone's current notifications, first call open_android_capabilities. "
    "Only when status is READY, call open_android_device using the returned capabilityId, capabilityVersion "
    "and schema-conforming parameters for the paired phone. "
    "Do not fall back to terminal or adb devices: these run on the Agent host, outside Gateway device routing. "
    "Otherwise report the exact status/reason and guide the user: "
    "GATEWAY_DISABLED: enable the account switch in hermes gateway setup; "
    "PLUGIN_NOT_INSTALLED: use the Full App's 安装通知读取插件 entry in plugin settings for notifications, "
    "or install the matching signed .alp for other capabilities; "
    "PLUGIN_DISABLED: enable the plugin; "
    "LOCAL_GRANT_REQUIRED: grant the current pairing/local collection/app scope as the reason specifies; "
    "ANDROID_PERMISSION_REQUIRED: grant Android notification access or the indicated permission; "
    "PROVIDER_NOT_SELECTED: select the provider; PROVIDER_UNAVAILABLE: check the collector/listener connection; "
    "SYNC_REQUIRED: reconnect and synchronize the App; "
    "ANDROID_STATE_UNKNOWN: check App plugin-loading/sync diagnostics and update if needed; never claim the plugin is absent. "
    "Discover again after settings change."
)

TOOL_SCHEMA = {"name":TOOL_NAME,"description":"Invoke a capability enabled for this Gateway account and explicitly granted on Android. The host selects the device and provider. " + DEVICE_ROUTING_GUIDANCE,
    "parameters":{"type":"object","additionalProperties":False,"required":["capabilityId","capabilityVersion","parameters"],
        "properties":{"capabilityId":{"type":"string","maxLength":256},"capabilityVersion":{"type":"string","pattern":r"^\d+\.\d+\.\d+$"},"parameters":{"type":"object"}}}}

DISCOVERY_SCHEMA = {"name": "open_android_capabilities", "description": "List this conversation's paired Android capabilities, exact parameter schemas, Gateway switches and Android authorization diagnostics. " + DEVICE_ROUTING_GUIDANCE,
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}


def discover(core: Any, args: Mapping[str, Any], **host_context: Any) -> dict[str, Any]:
    from .core import GatewayError
    from .capability_policy import DeviceCapabilityPolicy, capability_catalog
    if not isinstance(args, Mapping) or args:
        raise GatewayError("SCHEMA_INVALID")
    origin = resolve_origin(core, str(host_context.get("session_id") or ""))
    if origin is None:
        raise GatewayError("PAIRING_REQUIRED")
    account = core.open_gateway_account(origin["accountId"])
    try:
        device = account.store.database.execute("SELECT pairing_generation FROM device_keys WHERE device_id=?", (origin["deviceId"],)).fetchone()
        if not device or device[0] != origin["pairingGeneration"]:
            raise GatewayError("PAIRING_GENERATION_STALE")
        return {"policyRevision": DeviceCapabilityPolicy(account.store).snapshot()["revision"],
                "capabilities": capability_catalog(account, origin["deviceId"])}
    finally:
        account.close()

def resolve_origin(core: Any,session_id: str,account_hint: str | None = None,conversation_hint: str | None = None) -> dict[str, Any] | None:
    turn = trusted_turn.get()
    if turn is not None and turn.get("core") is core and (not session_id or turn.get("sessionId") == session_id):
        return turn
    history = getattr(core.history_reader,"__self__",None)
    db = getattr(getattr(history,"session_store",None),"_db",None)
    if db is None: return None
    for account_id in ([account_hint] if account_hint else core.list_gateway_account_ids()):
        account = core.open_gateway_account(account_id)
        try:
            if conversation_hint:
                binding = account.agent_sessions.lookup(conversation_hint)
                if not binding or not binding.get("agentSessionId"): continue
                candidates = [(conversation_hint,binding["agentSessionId"])]
            else:
                candidates = [(row[0],row[1]) for row in account.store.database.execute(
                    "SELECT conversation_id,agent_session_id FROM conversation_agent_sessions WHERE agent_session_id=?",(session_id,)).fetchall()]
            for conversation_id,native_id in candidates:
                if session_id and native_id != session_id: continue
                messages = db.get_messages(native_id)
                user = next((row for row in reversed(messages) if row.get("role") == "user"),None)
                platform_id = user.get("platform_message_id") if user else None
                if not platform_id: continue
                origin = account.store.database.execute("SELECT value FROM account_metadata WHERE key=?",(f"message-device:{platform_id}",)).fetchone()
                if origin:
                    result = json.loads(origin[0])
                    if result["conversationId"] != conversation_id: continue
                    return {**result,"accountId":account_id,"sessionId":native_id,"core":core}
        finally: account.close()
    return None

def _stop_waiting(core: Any, origin: Mapping[str, Any], request_id: str) -> bool:
    from .core import _now
    account = core.open_gateway_account(origin["accountId"])
    try:
        with account.store.transaction():
            account.device_requests._expire_if_due(account.device_requests._row(request_id), _now())
            if account.device_requests.get(request_id)["state"] in {"pending", "claimed"}:
                account.device_requests.cancel(request_id,origin["deviceId"],origin["pairingGeneration"],origin["grantRevision"],origin["messageId"])
            return account.store.database.execute("SELECT 1 FROM claim_receipts WHERE request_id=?",(request_id,)).fetchone() is not None
    finally:
        account.close()

async def execute(core: Any,args: Mapping[str, Any],**host_context: Any) -> dict[str, Any]:
    from .core import GatewayError, _jcs, _now
    if (not isinstance(args,Mapping) or set(args) != {"capabilityId","capabilityVersion","parameters"} or not isinstance(args["parameters"],dict)
            or not isinstance(args["capabilityId"],str) or not isinstance(args["capabilityVersion"],str)):
        raise GatewayError("SCHEMA_INVALID")
    origin = resolve_origin(core,str(host_context.get("session_id") or ""))
    if origin is None: raise GatewayError("PAIRING_REQUIRED")
    account_id,device_id = origin["accountId"],origin["deviceId"]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + DEVICE_TOOL_WAIT_SECONDS
    account = core.open_gateway_account(account_id)
    request_id = f"devreq_{uuid.uuid4()}"
    try:
        device = account.store.database.execute("SELECT pairing_generation,grant_revision FROM device_keys WHERE device_id=?",(device_id,)).fetchone()
        if not device or int(device[0]) != origin["pairingGeneration"] or int(device[1]) != origin["grantRevision"]:
            raise GatewayError("GRANT_STALE")
        bindings = account.device_requests.capabilities.available(device_id,int(device[0]),int(device[1]))
        binding = next((item for item in bindings if item["capabilityId"] == args["capabilityId"] and item["capabilityVersion"] == args["capabilityVersion"]),None)
        if binding is None:
            from .capability_policy import capability_catalog, DeviceCapabilityPolicy
            diagnostics = [item for item in capability_catalog(account,device_id) if item["capabilityId"] == args["capabilityId"]]
            reason = ("GATEWAY_DISABLED" if not DeviceCapabilityPolicy(account.store).allows(args["capabilityId"]) else
                      next((item["status"] for item in diagnostics if item["capabilityVersion"] in {None,args["capabilityVersion"]}),"CAPABILITY_NOT_PUBLISHED"))
            raise GatewayError("CAPABILITY_DENIED", {"reason":reason,"capabilities":diagnostics})
        queued = account.device_requests.enqueue(request_id,device_id,int(device[0]),int(device[1]),binding["risk"],
            {"id":binding["capabilityId"],"version":binding["capabilityVersion"]},{"pluginId":binding["pluginId"],"authorKeyId":binding["authorKeyId"]},
            args["parameters"],origin["messageId"],requires_foreground_confirmation=binding["risk"] in {"write","high-privilege-ephemeral"},
            online=core.is_device_online(account_id,device_id,int(device[0])),
            timeout_seconds=DEVICE_REQUEST_TIMEOUT_SECONDS)
        expires = _now(queued["expiresAt"]).timestamp()
    finally: account.close()
    stage = "claim_wait"
    try:
        while True:
            account = core.open_gateway_account(account_id)
            try:
                # 超时取消与结果读取共用事务，避免覆盖同时抵达的真实终态。
                with account.store.transaction():
                    current = account.store.database.execute("SELECT pairing_generation,grant_revision FROM device_keys WHERE device_id=?",(device_id,)).fetchone()
                    if not current or tuple(current) != (origin["pairingGeneration"],origin["grantRevision"]):
                        raise GatewayError("GRANT_STALE")
                    account.device_requests._assert_capability_enabled(account.device_requests._row(request_id))
                    account.device_requests._expire_if_due(account.device_requests._row(request_id), _now())
                    record = account.device_requests.get(request_id)
                    receipt = account.store.database.execute("SELECT claim_id FROM claim_receipts WHERE request_id=?",(request_id,)).fetchone()
                    stage = "result_wait" if receipt else "claim_wait"
                    if receipt and record["state"] in {"succeeded","failed","denied","cancelled","outcome_unknown"}:
                        result = account.device_requests.read_result(request_id,receipt[0])
                        response = {"requestId":request_id,**(result or {
                            "outcome":"outcome_unknown", "code":"DEVICE_TIMEOUT", "stage":stage})}
                        if result is not None:
                            fingerprint = hashlib.sha256(_jcs(response).encode()).hexdigest()
                            with _pending_lock: _pending[(id(core),str(origin.get("sessionId") or ""),request_id)] = (account_id,receipt[0],fingerprint)
                        logger.info("[open_android] device_request requestId=%s stage=tool_result state=%s", request_id, record["state"])
                        return response
                    if record["state"] == "cancelled":
                        return {"requestId":request_id,"outcome":"cancelled","code":"DEVICE_CANCELLED","stage":stage}
                    if record["state"] == "expired" or loop.time() >= deadline:
                        if record["state"] in {"pending", "claimed"}:
                            record = account.device_requests.cancel(request_id,device_id,origin["pairingGeneration"],origin["grantRevision"],origin["messageId"])
                        code = "DEVICE_TIMEOUT" if receipt or core.is_device_online(account_id,device_id,origin["pairingGeneration"]) else "DEVICE_OFFLINE"
                        logger.warning("[open_android] device_request requestId=%s stage=%s state=%s code=%s", request_id, stage, record["state"], code)
                        return {"requestId":request_id,"outcome":"outcome_unknown" if receipt else "failed","code":code,"stage":stage}
            finally: account.close()
            await asyncio.sleep(max(0, min(0.2, deadline - loop.time(), expires - _now().timestamp())))
    except GatewayError as exc:
        uncertain = stage == "result_wait"
        try:
            uncertain = _stop_waiting(core, origin, request_id)
            stage = "result_wait" if uncertain else "claim_wait"
        except Exception as cleanup_error:
            uncertain = True
            logger.warning("[open_android] device_request requestId=%s stage=cleanup errorType=%s", request_id, type(cleanup_error).__name__)
        logger.warning("[open_android] device_request requestId=%s stage=%s code=%s", request_id, stage, exc.code)
        raise GatewayError(exc.code, {**exc.details, "requestId":request_id, "stage":stage,
            "outcome":"outcome_unknown" if uncertain else "denied"}) from exc
    except asyncio.CancelledError:
        # 宿主停止也要阻止未领取请求；已领取请求不能伪称尚未产生副作用。
        try:
            _stop_waiting(core, origin, request_id)
        except Exception as exc:
            logger.warning("[open_android] device_request requestId=%s stage=host_cancel errorType=%s", request_id, type(exc).__name__)
        raise
    except Exception as exc:
        try:
            stage = "result_wait" if _stop_waiting(core, origin, request_id) else "claim_wait"
        except Exception as cleanup_error:
            logger.warning("[open_android] device_request requestId=%s stage=cleanup errorType=%s", request_id, type(cleanup_error).__name__)
        logger.error("[open_android] device_request requestId=%s stage=%s errorType=%s", request_id, stage, type(exc).__name__)
        raise GatewayError("INTERNAL_ERROR", {"requestId":request_id, "stage":stage, "outcome":"outcome_unknown"}) from exc

def acknowledge_adopted(core: Any,**event: Any) -> None:
    from .core import _jcs
    if event.get("tool_name") != TOOL_NAME or event.get("status") not in {None,"ok","success"}: return
    result = event.get("result")
    if isinstance(result,str):
        try: result = json.loads(result)
        except (TypeError,ValueError): return
    if not isinstance(result,dict) or not isinstance(result.get("requestId"),str): return
    key = (id(core),str(event.get("session_id") or ""),result["requestId"])
    with _pending_lock: pending = _pending.get(key)
    if pending is None or hashlib.sha256(_jcs(result).encode()).hexdigest() != pending[2]: return
    account = core.open_gateway_account(pending[0])
    try: account.device_requests.acknowledge_result(result["requestId"],pending[1])
    finally: account.close()
    with _pending_lock: _pending.pop(key,None)

def register_device_tools(ctx: Any,core: Any) -> None:
    register = getattr(ctx,"register_tool",None)
    hook = getattr(ctx,"register_hook",None) or getattr(ctx,"on",None)
    if not callable(register) or not callable(hook): return
    async def handler(args: dict[str,Any],**kwargs: Any) -> str:
        from .core import _jcs, GatewayError
        try: return _jcs(await execute(core,args,**kwargs))
        except GatewayError as exc: return _jcs({"outcome":"denied","code":exc.code,**exc.details})
    async def discovery_handler(args: dict[str,Any],**kwargs: Any) -> str:
        from .core import _jcs, GatewayError
        try: return _jcs(discover(core,args,**kwargs))
        except GatewayError as exc: return _jcs({"code":exc.code,**exc.details})
    register(name=TOOL_NAME,toolset="open_android",schema=TOOL_SCHEMA,handler=handler,is_async=True,description=TOOL_SCHEMA["description"],emoji="📱")
    register(name=DISCOVERY_SCHEMA["name"],toolset="open_android",schema=DISCOVERY_SCHEMA,handler=discovery_handler,is_async=True,description=DISCOVERY_SCHEMA["description"],emoji="📱")
    hook("post_tool_call",lambda **event: acknowledge_adopted(core,**event))
