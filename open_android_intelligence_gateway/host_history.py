"""History stays in Hermes' SessionDB; Gateway stores only stable wire bindings.

The session ID is read from the account's host-created binding. Neither an HTTP
request nor a model parameter may supply a database path or select a host session.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone, timedelta
import threading
from typing import Any, Mapping
from .history_media import HistoryMedia


def visible_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content
                       if isinstance(part, Mapping) and part.get("type") == "text")
    return ""


PRESENTATION_KIND = "oai_gateway_presentation_v1"
PRESENTATION_KEY = "oaiGatewayPresentation"


def _message_parts(account: Any, row: Any, text: str) -> list[dict[str, Any]]:
    parts = [{"type": "text", "text": text}] if text else []
    media = HistoryMedia(account)
    for attachment_id in json.loads(row["attachment_ids_json"] or "[]"):
        part = media.upload_part(row["conversation_id"], row["message_id"], attachment_id)
        if part is not None:
            parts.append(part)
    return parts


def _deleted(row: Mapping[str, Any]) -> bool:
    # 压缩归档仍属于历史；只有真正撤回的原生记录成为删除事实。
    return row.get("active", 1) in (0, False) and not row.get("compacted", 0)


def _wire(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    names = {"messageId", "clientMessageId", "conversationId", "sender", "text", "parts",
             "timestamp", "state", "batchId"}
    return {key: value for key, value in snapshot.items() if key in names}


def _tool_activity(row: Mapping[str, Any]) -> str:
    calls = row.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        return ""
    names = []
    for call in calls:
        function = call.get("function") if isinstance(call, Mapping) else None
        name = function.get("name") if isinstance(function, Mapping) else None
        if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9._~-]{1,128}", name):
            names.append(name)
    # 旧进度文字已丢失时只说明真实调用名称，不猜测参数或执行结果。
    return "调用工具：" + "、".join(names) if names else "工具调用"


class HermesHistoryPort:
    def __init__(self, core: Any, session_store: Any):
        self.core = core
        self.session_store = session_store
        self.storage_owner = hashlib.sha256(str(core.storage_root.resolve()).encode()).hexdigest()
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        self._migration_lock = threading.RLock()

    def _database(self, session_id: str) -> Any:
        resolve = getattr(self.session_store, "_db_for_session_id", None)
        database = resolve(session_id) if callable(resolve) else getattr(self.session_store, "_db", None)
        if database is None or not callable(getattr(database, "get_messages", None)):
            raise RuntimeError("HOST_HISTORY_UNAVAILABLE")
        return database

    @staticmethod
    def _session_chain(database: Any, session_id: str) -> list[str]:
        lineage = getattr(database, "get_compression_lineage", None)
        chain = getattr(database, "get_compression_chain", None)
        ancestors = lineage(session_id) if callable(lineage) else [session_id]
        # 跟随宿主合法压缩链，分支和重置会话由原生 API 排除。
        return chain(ancestors[0]) if ancestors and callable(chain) else (ancestors or [session_id])

    def _history_rows(self, database: Any, session_id: str) -> list[dict[str, Any]]:
        rows = [row for owner in self._session_chain(database, session_id)
                for row in database.get_messages(owner, include_inactive=True)]
        return sorted(rows, key=lambda row: row["id"])

    @staticmethod
    def _native_key(session_id: str, row: Mapping[str, Any]) -> str:
        owner = row.get("session_id") or session_id
        return "history-native:" + hashlib.sha256(f"{owner}:{row['id']}".encode()).hexdigest()

    def _presentations(self, account_id: str, conversation_id: str, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        result = {}
        for row in rows:
            if row.get("role") != "session_meta" or row.get("display_kind") != PRESENTATION_KIND:
                continue
            metadata = row.get("display_metadata")
            snapshot = metadata.get(PRESENTATION_KEY) if isinstance(metadata, Mapping) else None
            if (not isinstance(snapshot, dict) or snapshot.get("version") != 1
                    or snapshot.get("accountId") != account_id or snapshot.get("conversationId") != conversation_id
                    or snapshot.get("storageOwner") != self.storage_owner
                    or not isinstance(snapshot.get("messageId"), str) or snapshot.get("sender") not in {"user", "assistant"}
                    or not isinstance(snapshot.get("text"), str) or not isinstance(snapshot.get("parts"), list)):
                continue
            snapshot = dict(snapshot)
            if _deleted(row):
                snapshot.update(state="DELETED", text="", parts=[])
            existing = result.get(snapshot["messageId"])
            if existing is None or snapshot.get("revision", 0) >= existing.get("revision", 0):
                result[snapshot["messageId"]] = snapshot
        return result

    def _save(self, account: Any, database: Any, session_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        from .core import _jcs
        marker = "oai_display_" + hashlib.sha256(
            _jcs([self.storage_owner, account.account_id, snapshot["conversationId"], snapshot["messageId"]]).encode()).hexdigest()
        key = "history-presentation:" + snapshot["conversationId"] + ":" + snapshot["messageId"]
        saved = account.store.database.execute("SELECT value FROM account_metadata WHERE key=?", (key,)).fetchone()
        index = json.loads(saved[0]) if saved else {}
        if index:
            snapshot["timestamp"] = index["timestamp"]
        fingerprint = hashlib.sha256(_jcs({**snapshot, "storageOwner": self.storage_owner}).encode()).hexdigest()
        if index.get("fingerprint") == fingerprint and index.get("sessionId") == session_id:
            return {**snapshot, "revision": index["revision"]}
        revision = int(index.get("revision", 0)) + 1
        stored = {**snapshot, "version": 1, "accountId": account.account_id,
                  "storageOwner": self.storage_owner, "revision": revision}
        update = getattr(database, "set_latest_matching_message_display_kind", None)
        updated = bool(index.get("sessionId") == session_id and callable(update) and update(
            session_id, role="session_meta", content=marker, display_kind=PRESENTATION_KIND,
            display_metadata={PRESENTATION_KEY: stored},
        ))
        if not updated:
            database.append_message(session_id, "session_meta", marker,
                platform_message_id=marker, timestamp=snapshot["timestamp"] / 1000,
                display_kind=PRESENTATION_KIND, display_metadata={PRESENTATION_KEY: stored})
        # Gateway 索引只含身份、修订和摘要；长期正文仅保存在原生 SessionDB。
        account.store.database.execute("INSERT OR REPLACE INTO account_metadata(key,value) VALUES (?,?)", (key,
            json.dumps({"sessionId": session_id, "revision": revision, "timestamp": stored["timestamp"],
                        "fingerprint": fingerprint}, separators=(",", ":"))))
        return stored

    def capture_submission(self, account: Any, dispatch: Mapping[str, Any]) -> None:
        binding = account.agent_sessions.lookup(dispatch["conversationId"])
        if not binding or not binding.get("agentSessionId"):
            raise RuntimeError("HOST_HISTORY_UNAVAILABLE")
        session_id = binding["agentSessionId"]
        database = self._database(session_id)
        session_id = self._session_chain(database, session_id)[-1]
        generation = account.conversations.workflow.for_message(dispatch["messageId"])
        message_ids = generation["messageIds"] if generation and generation.get("offsets") else [dispatch["messageId"]]
        with self._migration_lock:
            for message_id in message_ids:
                row = account.store.database.execute("SELECT * FROM messages WHERE message_id=? AND conversation_id=?",
                    (message_id, dispatch["conversationId"])).fetchone()
                if row is None or not row["text"]:
                    continue
                text = account.store.open_json(row["text"], f"message:{message_id}:text")
                timestamp = int(datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).timestamp() * 1000)
                snapshot = {"messageId": message_id, "clientMessageId": row["client_message_id"],
                    "conversationId": row["conversation_id"], "sender": "user", "text": text,
                    "parts": _message_parts(account, row, text), "timestamp": timestamp, "state": row["state"],
                    "anchorClientMessageId": dispatch["clientMessageId"]}
                if generation and generation.get("offsets"):
                    snapshot["batchId"] = generation["batchId"]
                self._save(account, database, session_id, snapshot)

    def record_visible(self, account: Any, payload: Mapping[str, Any], state: str = "CONFIRMED") -> dict[str, Any] | None:
        if not visible_text(payload.get("parts")) and not any(
                part.get("type") != "text" for part in payload.get("parts", [])) and state != "DELETED":
            return None
        binding = account.agent_sessions.lookup(payload["conversationId"])
        if not binding or not binding.get("agentSessionId"):
            raise RuntimeError("HOST_HISTORY_UNAVAILABLE")
        session_id = binding["agentSessionId"]
        database = self._database(session_id)
        session_id = self._session_chain(database, session_id)[-1]
        snapshot = {"messageId": payload["messageId"], "clientMessageId": payload.get("clientMessageId"),
            "conversationId": payload["conversationId"], "sender": payload.get("sender", "assistant"),
            "text": payload.get("text", visible_text(payload.get("parts"))), "parts": list(payload.get("parts", [])),
            "timestamp": payload["timestamp"], "state": state}
        if payload.get("anchorClientMessageId"):
            snapshot["anchorClientMessageId"] = payload["anchorClientMessageId"]
        if state == "DELETED":
            snapshot.update(text="", parts=[])
        with self._migration_lock:
            stored = self._save(account, database, session_id, snapshot)
            if snapshot["sender"] == "assistant" and snapshot["text"]:
                # 匹配本轮已落库的回答；进度通知没有模型记录时只用展示身份。
                for row in reversed(self._history_rows(database, session_id)):
                    if row.get("role") != "assistant" or row.get("tool_calls") or _deleted(row) or visible_text(row.get("content")) != snapshot["text"]:
                        continue
                    key = self._native_key(session_id, row)
                    mapped = account.store.database.execute("SELECT value FROM account_metadata WHERE key=?", (key,)).fetchone()
                    if mapped and mapped[0] != snapshot["messageId"]:
                        continue
                    account.store.database.execute("INSERT OR IGNORE INTO account_metadata(key,value) VALUES (?,?)", (key, snapshot["messageId"]))
                    break
            return stored

    def read(self, account_id: str, conversation_id: str) -> list[dict[str, Any]]:
        account = self.core.open_gateway_account(account_id)
        try:
            binding = account.agent_sessions.lookup(conversation_id)
            if not binding or not binding.get("agentSessionId"):
                return []
            session_id = binding["agentSessionId"]
            database = self._database(session_id)
            with self._migration_lock, account.store.transaction():
                account.store.database.execute("INSERT OR IGNORE INTO account_metadata(key,value) VALUES ('native-history-migration-cutoff',?)", (self.started_at,))
                cutoff = account.store.database.execute("SELECT value FROM account_metadata WHERE key='native-history-migration-cutoff'").fetchone()[0]
                rows = self._history_rows(database, session_id)
                by_platform = {str(row["platform_message_id"]): row for row in rows if row.get("platform_message_id") and row.get("role") in {"user", "assistant"}}
                remaining = list(rows)
                queued = account.store.database.execute(
                    "SELECT m.* FROM messages m LEFT JOIN conversation_message_dispatch d ON d.message_id=m.message_id "
                    "WHERE m.conversation_id=? AND m.text!='' AND (m.sender='assistant' OR d.status IN ('delivered','completed')) "
                    "ORDER BY m.created_at,m.message_id LIMIT 1000", (conversation_id,),
                ).fetchall()
                for row in queued:
                    generation = account.conversations.workflow.for_message(row["message_id"])
                    if generation and generation.get("offsets") and row["message_id"] != generation["messageIds"][0]:
                        continue
                    text = account.store.open_json(row["text"], f"message:{row['message_id']}:text")
                    native = by_platform.get(row["client_message_id"])
                    if native is None and row["created_at"] <= cutoff:
                        native = next((item for item in remaining if item.get("role") == row["sender"] and visible_text(item.get("content")) == str(text)), None)
                        if native is None:
                            from .core import _now
                            native_id = database.append_message(session_id, row["sender"], str(text),
                                platform_message_id=row["client_message_id"], timestamp=_now(row["created_at"]).timestamp())
                            native = {"id": native_id, "platform_message_id": row["client_message_id"]}
                        else:
                            remaining.remove(native)
                    if native is None:
                        continue
                    if row["sender"] == "user":
                        self.capture_submission(account, {"conversationId": conversation_id,
                            "messageId": row["message_id"], "clientMessageId": row["client_message_id"]})
                    else:
                        self.record_visible(account, {"conversationId": conversation_id, "messageId": row["message_id"],
                            "sender": "assistant", "text": str(text), "parts": [{"type": "text", "text": str(text)}] + HistoryMedia(account).reply_parts(row["message_id"]),
                            "timestamp": int(datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).timestamp() * 1000)})
                    native_key = self._native_key(session_id, native)
                    account.store.database.execute("INSERT OR IGNORE INTO account_metadata(key,value) VALUES (?,?)", (native_key, row["message_id"]))
                    for mid in generation["messageIds"] if generation and generation.get("offsets") else [row["message_id"]]:
                        account.store.database.execute("UPDATE messages SET text='' WHERE message_id=?", (mid,))
                rows = self._history_rows(database, session_id)

            presentations = self._presentations(account_id, conversation_id, rows)
            native_by_client = {str(row["platform_message_id"]): row for row in rows if row.get("role") == "user" and row.get("platform_message_id")}
            result = {}
            for row in rows:
                role = row.get("role")
                if role not in {"user", "assistant"} or row.get("_compressed_summary"):
                    continue
                text = visible_text(row.get("content"))
                native_id = f"{session_id}:{row['id']}"
                native_key = self._native_key(session_id, row)
                mapped = account.store.database.execute("SELECT value FROM account_metadata WHERE key=?", (native_key,)).fetchone()
                client_id = row.get("platform_message_id")
                protocol = account.store.database.execute(
                    "SELECT * FROM messages WHERE conversation_id=? AND client_message_id=? AND sender=?",
                    (conversation_id, client_id, role),
                ).fetchone() if client_id else None
                if mapped:
                    message_id = mapped[0]
                elif protocol:
                    message_id = protocol["message_id"]
                else:
                    digest = hashlib.sha256(text.encode()).hexdigest()
                    prefix = f"history-hash:{conversation_id}:"
                    candidate = account.store.database.execute("SELECT key FROM account_metadata WHERE key LIKE ? AND value=? ORDER BY key LIMIT 1", (prefix + "%", digest)).fetchone()
                    message_id = candidate[0][len(prefix):] if candidate else "msg_host_" + hashlib.sha256(f"{account_id}:{conversation_id}:{native_id}".encode()).hexdigest()[:40]
                    if candidate:
                        account.store.database.execute("DELETE FROM account_metadata WHERE key=?", (candidate[0],))
                account.store.database.execute("INSERT OR IGNORE INTO account_metadata(key,value) VALUES (?,?)", (native_key, message_id))
                timestamp = row.get("timestamp")
                try:
                    millis = int(float(timestamp) * 1000) if isinstance(timestamp, (float, int)) else int(datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).timestamp() * 1000)
                except (ValueError, TypeError):
                    millis = int(row["id"])
                snapshot = presentations.get(message_id)
                if snapshot is not None:
                    item = _wire(snapshot)
                else:
                    # 只对确定由 Gateway 接收的用户消息使用原稿兜底；其他原生旧纯文本仍可读。
                    owned_user = role == "user" and (protocol is not None or account.store.database.execute(
                        "SELECT 1 FROM messages WHERE conversation_id=? AND message_id=? AND sender='user'",
                        (conversation_id, message_id)).fetchone() is not None)
                    # 原稿已被旧版本清除时不猜测剥离模型前缀，端侧按稳定身份恢复原发送记录。
                    display_text = "" if owned_user else text
                    reply_parts = HistoryMedia(account).reply_parts(message_id) if role == "assistant" else []
                    if role == "assistant" and not text and not reply_parts:
                        text = _tool_activity(row)
                        if not text:
                            continue
                    item = {"messageId": message_id, "clientMessageId": client_id, "conversationId": conversation_id,
                        "sender": role, "text": display_text if role == "user" else text,
                        "parts": ([{"type": "text", "text": display_text}] if display_text else []) if role == "user"
                            else ([{"type": "text", "text": text}] if text else []) + reply_parts,
                        "timestamp": millis, "state": "CONFIRMED"}
                if _deleted(row):
                    item.update(state="DELETED", text="", parts=[])
                result[message_id] = item
            for message_id, snapshot in presentations.items():
                item = _wire(snapshot)
                native = native_by_client.get(snapshot.get("anchorClientMessageId"))
                if native is not None and _deleted(native):
                    item.update(state="DELETED", text="", parts=[])
                if message_id in result and result[message_id].get("state") == "DELETED":
                    item.update(state="DELETED", text="", parts=[])
                result[message_id] = item
            for item in result.values():
                if item["state"] == "DELETED":
                    HistoryMedia(account).mark_message_deleted(item["messageId"])
            return [expanded for item in result.values() for expanded in
                    ([item] if item.get("batchId") else account.conversations.workflow.expand_history(item))]
        finally:
            account.close()

    def maintain(self, account_id: str, now: datetime | None = None) -> None:
        account = self.core.open_gateway_account(account_id)
        try:
            saved = account.store.database.execute("SELECT value FROM account_metadata WHERE key='native-history-maintenance-offset'").fetchone()
            offset = int(saved[0]) if saved else 0
            conversations = account.store.database.execute("SELECT conversation_id FROM conversation_agent_sessions WHERE agent_session_id IS NOT NULL ORDER BY conversation_id LIMIT 10 OFFSET ?", (offset,)).fetchall()
        finally:
            account.close()
        for row in conversations:
            self.read(account_id, row[0])
        account = self.core.open_gateway_account(account_id)
        try:
            next_offset = offset + len(conversations) if len(conversations) == 10 else 0
            account.store.database.execute("INSERT OR REPLACE INTO account_metadata(key,value) VALUES ('native-history-maintenance-offset',?)", (str(next_offset),))
            from .core import iso_millis
            cutoff = iso_millis((now or datetime.now(timezone.utc)) - timedelta(seconds=self.core.attachment_policy.attachment_ttl_seconds))
            migration = account.store.database.execute("SELECT value FROM account_metadata WHERE key='native-history-migration-cutoff'").fetchone()
            legacy_cutoff = migration[0] if migration else self.started_at
            account.store.database.execute("UPDATE messages SET text='' WHERE message_id IN (SELECT m.message_id FROM messages m LEFT JOIN conversation_message_dispatch d ON d.message_id=m.message_id WHERE m.text!='' AND m.created_at<=? AND (m.created_at>? OR (m.sender='user' AND (d.status IS NULL OR d.status NOT IN ('delivered','completed')))) LIMIT 1000)", (cutoff, legacy_cutoff))
        finally:
            account.close()

    def cleanup_account(self, account_id: str) -> None:
        account = self.core.open_gateway_account(account_id)
        try:
            bindings = {row[0] for row in account.store.database.execute("SELECT agent_session_id FROM conversation_agent_sessions WHERE agent_session_id IS NOT NULL")}
            sessions = {session_id for binding in bindings for session_id in self._session_chain(self._database(binding), binding)}
        finally:
            account.close()
        # 先核对没有另一个账号绑定同一会话，避免异常旧绑定扩大删除范围。
        for other_id in self.core.list_gateway_account_ids():
            if other_id == account_id:
                continue
            other = self.core.open_gateway_account(other_id)
            try:
                if any(row[0] in sessions for row in other.store.database.execute("SELECT agent_session_id FROM conversation_agent_sessions WHERE agent_session_id IS NOT NULL")):
                    raise RuntimeError("HOST_HISTORY_BINDING_CONFLICT")
            finally:
                other.close()
        with self._migration_lock:
            for session_id in sessions:
                database = self._database(session_id)
                write = getattr(database, "_execute_write", None)
                if not callable(write):
                    raise RuntimeError("HOST_HISTORY_UNAVAILABLE")
                def clear_owned(conn):
                    # 使用宿主写事务，范围只包含本插件、此账号的展示侧记录。
                    conn.execute("DELETE FROM messages WHERE session_id=? AND role='session_meta' "
                        "AND display_kind=? AND json_valid(display_metadata) "
                        "AND json_extract(display_metadata,'$.oaiGatewayPresentation.version')=1 "
                        "AND json_extract(display_metadata,'$.oaiGatewayPresentation.accountId')=? "
                        "AND json_extract(display_metadata,'$.oaiGatewayPresentation.storageOwner')=?",
                        (session_id, PRESENTATION_KIND, account_id, self.storage_owner))
                    count, calls = database._active_transcript_counts(conn, session_id)
                    conn.execute("UPDATE sessions SET message_count=?,tool_call_count=? WHERE id=?",
                        (count, calls, session_id))
                write(clear_owned)
            HistoryMedia.retire_account_media(account_id, self.core.storage_root)


def host_history_page(port: Any, conversation_id: str, client_id: str | None, cursor: str | None, limit: int) -> dict[str, Any]:
    from .core import GatewayError, _jcs
    messages = port.history_reader(port.account_id, conversation_id)
    by_id = {row["messageId"]: row for row in messages}
    # Only unadopted transport messages remain in the Gateway body queue.
    pending = port.store.database.execute("SELECT * FROM messages WHERE conversation_id=? AND text!=''", (conversation_id,)).fetchall()
    for row in pending:
        if row["message_id"] not in by_id:
            text = port.store.open_json(row["text"], f"message:{row['message_id']}:text")
            by_id[row["message_id"]] = {"messageId": row["message_id"], "clientMessageId": row["client_message_id"],
                "conversationId": conversation_id, "sender": row["sender"], "text": text,
                "parts": [{"type": "text", "text": text}], "createdAt": row["created_at"],
                "timestamp": int(datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).timestamp()*1000), "state": row["state"]}
    if client_id and not any(row.get("clientMessageId") == client_id for row in by_id.values()):
        known = port.store.database.execute("SELECT * FROM messages WHERE conversation_id=? AND client_message_id=?",(conversation_id,client_id)).fetchone()
        if known:
            by_id[known["message_id"]] = {"messageId":known["message_id"],"clientMessageId":client_id,"conversationId":conversation_id,
                "sender":known["sender"],"text":"","parts":[],"timestamp":int(datetime.fromisoformat(known["created_at"].replace("Z","+00:00")).timestamp()*1000),"state":known["state"]}
    rows = sorted(by_id.values(), key=lambda row: (row["timestamp"], row["messageId"]))
    revision = int(hashlib.sha256(_jcs(rows).encode()).hexdigest()[:13], 16) + 1
    offset = 0
    if cursor:
        saved = port.store.open_json(cursor, f"host-timeline:{conversation_id}")
        if saved.get("revision") != revision:
            raise GatewayError("CURSOR_EXPIRED", {"recoverableResources": ["conversations"]})
        offset = saved.get("offset")
        if not isinstance(offset, int) or offset < 0 or offset > len(rows):
            raise GatewayError("SCHEMA_INVALID")
    if client_id:
        rows = [row for row in rows if row.get("clientMessageId") == client_id]
    size = max(1, min(int(limit), 100))
    end = offset + size
    next_cursor = port.store.seal_json({"offset": end, "revision": revision}, f"host-timeline:{conversation_id}") if end < len(rows) else None
    return {"messages": rows[offset:end], "nextCursor": next_cursor, "snapshotRevision": revision}
