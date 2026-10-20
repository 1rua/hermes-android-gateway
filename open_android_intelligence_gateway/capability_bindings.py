"""Device schemas attested by the authenticated Android pairing, never test fixtures."""
from __future__ import annotations
import hashlib
import json
import re
from typing import Any, Mapping
from jsonschema import Draft202012Validator, FormatChecker


class CapabilityBindings:
    def __init__(self, store: Any, contracts: Any):
        self.store, self.contracts = store, contracts

    def register(self, device_id: str, generation: int, revision: int, bindings: list[dict[str, Any]], inventory: list[dict[str, Any]] | None = None) -> None:
        from .core import _jcs
        self.validate_publication(bindings)
        if inventory is not None:
            self.validate_inventory(inventory, bindings)
        self.store.database.execute("INSERT OR REPLACE INTO account_metadata(key,value) VALUES (?,?)",
            (f"device-capabilities:{device_id}", _jcs({"generation": generation, "revision": revision, "bindings": bindings,
                                                    **({"inventory": inventory} if inventory is not None else {})})))

    def validate_inventory(self, inventory: Any, bindings: list[dict[str, Any]] | None = None) -> None:
        from .core import GatewayError, _jcs
        if not isinstance(inventory, list) or len(inventory) > 128 or len(_jcs(inventory).encode()) > 131072:
            raise GatewayError("SCHEMA_INVALID")
        seen = set()
        for item in inventory:
            if (not isinstance(item, dict) or set(item) != {"pluginId", "capabilityId", "capabilityVersion", "status", "reason"}
                    or any(not isinstance(v, str) for v in item.values())
                    or not re.fullmatch(r"[A-Za-z0-9.-]+", item["pluginId"])
                    or not item["capabilityId"].startswith(item["pluginId"] + ".")
                    or len(item["capabilityId"]) > 256
                    or not re.fullmatch(r"\d+\.\d+\.\d+", item["capabilityVersion"])
                    or item["status"] not in {"READY", "PLUGIN_DISABLED", "LOCAL_GRANT_REQUIRED", "ANDROID_PERMISSION_REQUIRED", "PROVIDER_UNAVAILABLE", "PROVIDER_NOT_SELECTED"}
                    or not re.fullmatch(r"[A-Z0-9_]{0,128}", item["reason"])):
                raise GatewayError("SCHEMA_INVALID")
            key = (item["pluginId"], item["capabilityId"], item["capabilityVersion"])
            if key in seen:
                raise GatewayError("SCHEMA_INVALID")
            seen.add(key)
        if bindings is not None:
            identity = lambda item: (item["pluginId"], item["capabilityId"], item["capabilityVersion"])
            if {identity(item) for item in inventory if item["status"] == "READY"} != {identity(item) for item in bindings}:
                raise GatewayError("SCHEMA_INVALID")

    def validate_publication(self, bindings: list[dict[str, Any]]) -> None:
        """Check the entire publication before making any authorization change."""
        from .core import GatewayError, _jcs
        if not isinstance(bindings, list) or len(bindings) > 128 or len(_jcs(bindings).encode()) > 262144:
            raise GatewayError("SCHEMA_INVALID")
        keys = set()
        for binding in bindings:
            if not isinstance(binding, dict) or set(binding) != {"pluginId", "authorKeyId", "capabilityId", "capabilityVersion", "schemaSha256", "schema", "risk"}:
                raise GatewayError("SCHEMA_INVALID")
            if any(not isinstance(binding[key],str) for key in set(binding)-{"schema"}):
                raise GatewayError("SCHEMA_INVALID")
            if (not re.fullmatch(r"[A-Za-z0-9.-]+", str(binding["pluginId"]))
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(binding["authorKeyId"]))
                or not str(binding["capabilityId"]).startswith(binding["pluginId"] + ".")
                or not re.fullmatch(r"\d+\.\d+\.\d+", str(binding["capabilityVersion"]))
                or binding["risk"] not in {"read", "sync", "write", "high-privilege-ephemeral"}
                or binding["schemaSha256"] != "sha256:" + hashlib.sha256(_jcs(binding["schema"]).encode()).hexdigest()):
                raise GatewayError("SCHEMA_INVALID")
            key = (binding["capabilityId"], binding["capabilityVersion"])
            if key in keys:
                raise GatewayError("SCHEMA_INVALID")
            keys.add(key)
            self._check_schema(binding["schema"])

    def _check_schema(self, root: Any) -> None:
        from .core import GatewayError
        if not isinstance(root, dict) or root.get("type") != "object" or root.get("additionalProperties") is not False:
            raise GatewayError("SCHEMA_INVALID")
        allowed = {"type","properties","required","additionalProperties","enum","const","minLength","maxLength",
                   "pattern","format","minimum","maximum","exclusiveMinimum","exclusiveMaximum","multipleOf",
                   "minItems","maxItems","uniqueItems","items","allOf","anyOf","oneOf","$defs","$ref","title","description","$schema","$id"}
        visited: set[int] = set()
        def check(schema: Any, depth: int) -> None:
            if isinstance(schema, bool):
                return
            if not isinstance(schema, dict) or depth > 32 or set(schema) - allowed:
                raise GatewayError("SCHEMA_INVALID")
            if id(schema) in visited:
                return
            visited.add(id(schema))
            if "$ref" in schema:
                if not str(schema["$ref"]).startswith("#/$defs/"):
                    raise GatewayError("SCHEMA_INVALID")
                target = root
                for token in str(schema["$ref"])[2:].split("/"):
                    if re.search(r"~(?![01])", token):
                        raise GatewayError("SCHEMA_INVALID")
                    token = token.replace("~1", "/").replace("~0", "~")
                    if not isinstance(target, dict) or token not in target:
                        raise GatewayError("SCHEMA_INVALID")
                    target = target[token]
                # References form a graph; only structural nesting consumes depth.
                check(target, 0)
            for name in ("properties","$defs"):
                for child in schema.get(name, {}).values(): check(child,depth+1)
            for name in ("items", "additionalProperties"):
                if isinstance(schema.get(name), dict): check(schema[name], depth+1)
            for name in ("allOf","anyOf","oneOf"):
                for child in schema.get(name, []): check(child,depth+1)
        check(root,0)
        try:
            Draft202012Validator.check_schema(root)
        except Exception:
            raise GatewayError("SCHEMA_INVALID") from None

    def publication(self, device_id: str) -> dict[str, Any] | None:
        row = self.store.database.execute("SELECT value FROM account_metadata WHERE key=?", (f"device-capabilities:{device_id}",)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, device_id: str, generation: int, revision: int) -> list[dict[str, Any]]:
        saved = self.publication(device_id)
        return saved["bindings"] if saved and saved["generation"] == generation and saved["revision"] == revision else []

    def available(self, device_id: str, generation: int, revision: int) -> list[dict[str, Any]]:
        from .capability_policy import DeviceCapabilityPolicy
        policy = DeviceCapabilityPolicy(self.store)
        return [binding for binding in self.list(device_id, generation, revision) if policy.allows(binding["capabilityId"])]

    def validate(self, device_id: str, generation: int, revision: int, request: Mapping[str, Any]) -> bool:
        if not self.contracts.validate("device.request", request): return False
        provider, capability = request["provider"], request["capability"]
        for binding in self.available(device_id, generation, revision):
            if (binding["pluginId"] == provider["pluginId"] and binding["authorKeyId"] == provider["authorKeyId"]
                and binding["capabilityId"] == capability["id"] and binding["capabilityVersion"] == capability["version"]):
                return binding["risk"] == request["risk"] and Draft202012Validator(binding["schema"],format_checker=FormatChecker()).is_valid(request["parameters"])
        return False
