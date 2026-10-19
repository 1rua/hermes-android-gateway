"""Account-scoped conversation organization and native lifecycle receipts.

The native SessionDB owns archive/delete. This database keeps only organization,
operation fences and tombstones so stale clients cannot recreate deleted chats.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


ORGANIZATION_KEY = "conversation-organization:v1"


class ConversationManagement:
    def __init__(self, conversations: Any):
        self.conversations = conversations
        self.store = conversations.store

    def _read(self, key: str, default: Any = None) -> Any:
        row = self.store.database.execute("SELECT value FROM account_metadata WHERE key=?", (key,)).fetchone()
        return self.store.open_json(row[0], key) if row else default

    def _write(self, key: str, value: Any) -> None:
        self.store.database.execute("INSERT OR REPLACE INTO account_metadata(key,value) VALUES (?,?)",
                                   (key, self.store.seal_json(value, key)))

    def state(self, conversation_id: str) -> dict[str, Any]:
        return self._read("conversation-management:" + conversation_id, {})

    def require_visible(self, conversation_id: str) -> None:
        from .core import GatewayError
        state = self.state(conversation_id)
        if state.get("deleted"):
            raise GatewayError("CONVERSATION_NOT_FOUND")
        if state.get("pending"):
            raise GatewayError("CONVERSATION_BUSY")

    def require_writable(self, conversation_id: str) -> None:
        from .core import GatewayError
        self.require_visible(conversation_id)
        reader = getattr(self.conversations.history_reader, "__self__", None)
        if callable(getattr(reader, "conversation_state", None)):
            self.record(conversation_id)
            self.require_visible(conversation_id)
        if self.state(conversation_id).get("archived"):
            raise GatewayError("CONVERSATION_ARCHIVED")

    def organization(self) -> dict[str, Any]:
        return self._read(ORGANIZATION_KEY, {"revision": 0, "sortMode": "updated-desc",
                                           "customOrder": [], "folders": [], "assignments": {}})

    def replace(self, body: dict[str, Any], correlation_id: str) -> dict[str, Any]:
        from .core import GatewayError
        with self.store.transaction():
            previous = self.organization()
            if previous["revision"] != body["expectedRevision"]:
                raise GatewayError("ORGANIZATION_CONFLICT", {"revision": previous["revision"]})
            ids = {row["conversationId"] for row in self.conversations.list()}
            folders = {row["folderId"] for row in body["folders"]}
            names = {row["name"].casefold() for row in body["folders"]}
            if (len(folders) != len(body["folders"]) or len(names) != len(folders)
                    or any(value not in ids for value in body["customOrder"])
                    or any(cid not in ids or folder not in folders for cid, folder in body["assignments"].items())):
                raise GatewayError("SCHEMA_INVALID")
            result = {key: body[key] for key in ("sortMode", "customOrder", "folders", "assignments")}
            result["revision"] = previous["revision"] + 1
            self._write(ORGANIZATION_KEY, result)
            self.conversations.events.append("conversation.organization.updated", correlation_id,
                                             {"revision": result["revision"]})
            return result

    def begin(self, conversation_id: str, request_id: str, operation: str, archived: bool | None) -> None:
        from .core import GatewayError
        with self.store.transaction():
            prior = self.state(conversation_id)
            if prior.get("deleted"):
                raise GatewayError("CONVERSATION_NOT_FOUND")
            if prior.get("pending"):
                if prior.get("pendingRequestId") != request_id:
                    raise GatewayError("OUTCOME_UNKNOWN")
                if prior.get("operation") != operation or prior.get("archiveValue") != archived:
                    raise GatewayError("IDEMPOTENCY_CONFLICT")
            else:
                self.conversations.get(conversation_id)
            active = self.conversations.workflow.current(conversation_id) if not prior.get("pending") else None
            if active and active["state"] in {"queued", "running", "unknown", "cancel_requested"}:
                raise GatewayError("CONVERSATION_BUSY")
            queued = self.store.database.execute("SELECT 1 FROM conversation_message_dispatch WHERE conversation_id=? "
                "AND status IN ('queued','dispatching','delivered') LIMIT 1", (conversation_id,)).fetchone()
            if queued:
                raise GatewayError("CONVERSATION_BUSY")
            self._write("conversation-management:" + conversation_id, {**prior, "pending": True,
                        "pendingRequestId": request_id, "operation": operation, "archiveValue": archived})

    def abort(self, conversation_id: str, request_id: str) -> None:
        state = self.state(conversation_id)
        if state.get("pendingRequestId") != request_id:
            return
        for name in ("pending", "pendingRequestId", "operation", "archiveValue"):
            state.pop(name, None)
        self._write("conversation-management:" + conversation_id, state)

    def commit(self, conversation_id: str, operation: str, archived: bool | None,
               correlation_id: str, now: Any = None) -> dict[str, Any]:
        from .core import _now, iso_millis
        with self.store.transaction():
            state = self.state(conversation_id)
            for name in ("pending", "pendingRequestId", "operation", "archiveValue"):
                state.pop(name, None)
            state["updatedAt"] = iso_millis(_now(now))
            state["deleted" if operation == "delete" else "archived"] = True if operation == "delete" else archived
            self._write("conversation-management:" + conversation_id, state)
            if operation == "delete":
                organization = self.organization()
                if conversation_id in organization["customOrder"] or conversation_id in organization["assignments"]:
                    organization["customOrder"] = [value for value in organization["customOrder"] if value != conversation_id]
                    organization["assignments"].pop(conversation_id, None)
                    organization["revision"] += 1
                    self._write(ORGANIZATION_KEY, organization)
                    self.conversations.events.append("conversation.organization.updated", correlation_id,
                                                     {"revision": organization["revision"]}, now)
            self.conversations.events.append("conversation.updated", correlation_id,
                {"conversationId": conversation_id, **({"deleted": True} if operation == "delete" else {"archived": archived})}, now)
            return {"conversationId": conversation_id, "deleted": True} if operation == "delete" else {"conversation": self.record(conversation_id)}

    def record(self, conversation_id: str) -> dict[str, Any]:
        from .core import GatewayError
        row = self.store.database.execute("SELECT c.*,MAX(m.created_at) AS last_message_at FROM conversations c "
            "LEFT JOIN messages m ON m.conversation_id=c.conversation_id WHERE c.conversation_id=? GROUP BY c.conversation_id",
            (conversation_id,)).fetchone()
        state = self.state(conversation_id)
        if row is None or state.get("deleted"):
            raise GatewayError("CONVERSATION_NOT_FOUND")
        reader = getattr(self.conversations.history_reader, "__self__", None)
        native_state = getattr(reader, "conversation_state", None)
        native = native_state(self.conversations.account_id, conversation_id, self.store) if callable(native_state) else None
        if native and native.get("deleted") and not state.get("pending"):
            self.commit(conversation_id, "delete", None, "native-conversation-sync")
            raise GatewayError("CONVERSATION_NOT_FOUND")
        if native and "archived" in native and not state.get("pending"):
            state = {**state, "archived": native["archived"]}
            self._write("conversation-management:" + conversation_id, state)
        timestamps = [row["created_at"], row["last_message_at"], state.get("updatedAt"), (native or {}).get("updatedAt")]
        return {"conversationId": conversation_id, "clientConversationId": row["client_conversation_id"],
                "title": row["title"], "archived": state.get("archived", False),
                "updatedAt": max(value for value in timestamps if value)}

    def page(self, cursor: str | None = None, limit: int | None = None) -> dict[str, Any]:
        from .core import GatewayError
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100):
            raise GatewayError("SCHEMA_INVALID")
        rows = []
        for conversation in self.conversations.list():
            try:
                rows.append(self.record(conversation["conversationId"]))
            except GatewayError as error:
                if error.code != "CONVERSATION_NOT_FOUND":
                    raise
        rows.sort(key=lambda row: row["conversationId"])
        rows.sort(key=lambda row: row["updatedAt"], reverse=True)
        revision = int(hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:13], 16) + 1
        offset = 0
        if cursor:
            try:
                saved = self.store.open_json(cursor, "conversation-list:v1")
                offset = saved["offset"]
                if saved["revision"] != revision:
                    raise GatewayError("CURSOR_EXPIRED", {"recoverableResources": ["conversations"]})
                if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= len(rows):
                    raise ValueError("invalid offset")
            except GatewayError as error:
                if error.code == "CURSOR_EXPIRED":
                    raise
                raise GatewayError("SCHEMA_INVALID") from error
            except (KeyError, TypeError, ValueError) as error:
                raise GatewayError("SCHEMA_INVALID") from error
        end = offset + (limit if limit is not None else len(rows))
        next_cursor = self.store.seal_json({"revision": revision, "offset": end}, "conversation-list:v1") if end < len(rows) else None
        return {"conversations": rows[offset:end], "snapshotRevision": revision, "nextCursor": next_cursor}
