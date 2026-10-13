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
PRESENTATION_NAMESPACE = "oai_gateway_presentation_v2:"


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
    result = {key: value for key, value in snapshot.items() if key in names}
    if isinstance(snapshot.get("revision"), int):
        result["contentRevision"] = snapshot["revision"]
    return result


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

    def _database(self, session_id: str, native_session_key: str | None = None) -> Any:
        # 压缩会推进路由，旧会话 ID 不能再用于猜测所属 profile 的数据库。
        resolve_key = getattr(self.session_store, "_db_for_key", None)
        resolve_id = getattr(self.session_store, "_db_for_session_id", None)
        if native_session_key and callable(resolve_key):
            database = resolve_key(native_session_key)
        else:
            database = resolve_id(session_id) if callable(resolve_id) else getattr(self.session_store, "_db", None)
        required = ("get_messages", "get_meta", "set_meta", "list_meta_prefix", "get_session")
        if database is None or any(not callable(getattr(database, name, None)) for name in required):
            raise RuntimeError("HOST_HISTORY_UNAVAILABLE")
        if database.get_session(session_id) is None:
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
        rows = [{**row, "session_id":owner} for owner in self._session_chain(database, session_id)
                for row in database.get_messages(owner, include_inactive=True)]
        return sorted(rows, key=lambda row: row["id"])

    @staticmethod
    def _native_key(session_id: str, row: Mapping[str, Any]) -> str:
        owner = row.get("session_id") or session_id
        return "history-native:" + hashlib.sha256(f"{owner}:{row['id']}".encode()).hexdigest()

    def _presentation_prefix(self, account_id: str, conversation_id: str | None = None) -> str:
        prefix = PRESENTATION_NAMESPACE + self.storage_owner + ":" + hashlib.sha256(account_id.encode()).hexdigest() + ":"
        return prefix if conversation_id is None else prefix + hashlib.sha256(conversation_id.encode()).hexdigest() + ":"

    def _presentation_key(self, account_id: str, conversation_id: str, message_id: str) -> str:
        return self._presentation_prefix(account_id, conversation_id) + hashlib.sha256(message_id.encode()).hexdigest()

    def _presentations(self, database: Any, account_id: str, conversation_id: str, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        result = {}
        # 兼容尚未部署的早期展示行，先在同一宿主事务迁移成功，再清除旧行。
        legacy = []
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
            legacy.append((row, dict(snapshot)))
        if legacy:
            write = getattr(database, "_execute_write", None)
            if not callable(write):
                raise RuntimeError("HOST_HISTORY_UNAVAILABLE")
            def migrate(conn):
                cursor = conn.cursor()
                for row, snapshot in legacy:
                    key = self._presentation_key(account_id, conversation_id, snapshot["messageId"])
                    previous = conn.execute("SELECT value FROM state_meta WHERE key=?", (key,)).fetchone()
                    saved = json.loads(previous[0]) if previous else None
                    if saved is None or saved.get("revision", 0) < snapshot.get("revision", 0):
                        database.set_meta(key, json.dumps({**snapshot, "version":2}, separators=(",", ":")), cursor=cursor)
                    conn.execute("DELETE FROM messages WHERE id=? AND role='session_meta' AND display_kind=?",
                                 (row["id"], PRESENTATION_KIND))
                for session in {row["session_id"] for row, _ in legacy}:
                    count, calls = database._active_transcript_counts(conn, session)
                    conn.execute("UPDATE sessions SET message_count=?,tool_call_count=? WHERE id=?", (count, calls, session))
            write(migrate)
        for _, value in database.list_meta_prefix(self._presentation_prefix(account_id, conversation_id)):
            snapshot = json.loads(value)
            if (snapshot.get("version") != 2 or snapshot.get("storageOwner") != self.storage_owner
                    or snapshot.get("accountId") != account_id or snapshot.get("conversationId") != conversation_id):
                continue
            result[snapshot["messageId"]] = snapshot
        return result

    def _save(self, account: Any, database: Any, session_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        from .core import _jcs
        key = "history-presentation:" + snapshot["conversationId"] + ":" + snapshot["messageId"]
        saved = account.store.database.execute("SELECT value FROM account_metadata WHERE key=?", (key,)).fetchone()
        index = json.loads(saved[0]) if saved else {}
        metadata_key = self._presentation_key(account.account_id, snapshot["conversationId"], snapshot["messageId"])
        native_value = database.get_meta(metadata_key)
        native = json.loads(native_value) if native_value else {}
        # 原生写入成功、短期索引尚未提交时，重试仍保持原始时间与递增版本。
        if native and (native.get("accountId") != account.account_id or native.get("storageOwner") != self.storage_owner):
            raise RuntimeError("HOST_HISTORY_BINDING_CONFLICT")
        if native:
            snapshot["timestamp"] = native["timestamp"]
        if index:
            snapshot["timestamp"] = index["timestamp"]
        fingerprint = hashlib.sha256(_jcs({**snapshot, "storageOwner": self.storage_owner}).encode()).hexdigest()
        if (index.get("fingerprint") == fingerprint and index.get("sessionId") == session_id
                and native.get("revision") == index.get("revision")):
            return {**snapshot, "revision": index["revision"]}
        revision = max(int(index.get("revision", 0)), int(native.get("revision", 0))) + 1
        stored = {**snapshot, "version": 2, "accountId": account.account_id,
                  "storageOwner": self.storage_owner, "revision": revision}
        # 宿主原生 KV 与模型 transcript 分开，不能改变压缩尾部和消息计数。
        database.set_meta(metadata_key, json.dumps(stored, separators=(",", ":")))
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
        database = self._database(session_id, binding.get("sessionKey"))
        session_id = self._session_chain(database, session_id)[-1]
        generation = account.conversations.workflow.for_message(dispatch["messageId"])
        message_ids = generation["messageIds"] if generation and generation.get("offsets") else [dispatch["messageId"]]
        with self._migration_lock:
            self._presentations(database, account.account_id, dispatch["conversationId"], self._history_rows(database, session_id))
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
        database = self._database(session_id, binding.get("sessionKey"))
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
            self._presentations(database, account.account_id, payload["conversationId"], self._history_rows(database, session_id))
            stored = self._save(account, database, session_id, snapshot)
            return stored

    def _bind_replies(self, account: Any, session_id: str, rows: list[dict[str, Any]], presentations: dict[str, dict[str, Any]]) -> None:
        # 正文只能用于本轮、明确用户锚点内的唯一候选，不能认领上一轮的同文回答。
        for snapshot in presentations.values():
            anchor_id = snapshot.get("anchorClientMessageId")
            if snapshot.get("sender") != "assistant" or snapshot.get("state") != "CONFIRMED" or not anchor_id:
                continue
            anchors = [row for row in rows if row.get("role") == "user" and not row.get("_compressed_summary")
                       and row.get("platform_message_id") == anchor_id and not _deleted(row)]
            if not anchors:
                continue
            anchor = max(anchors, key=lambda row: row["id"])
            end = min((row["id"] for row in rows if row.get("role") == "user" and not row.get("_compressed_summary")
                       and not _deleted(row) and row["id"] > anchor["id"]), default=float("inf"))
            peers = [other for other in presentations.values() if other.get("sender") == "assistant"
                     and other.get("state") == "CONFIRMED" and other.get("anchorClientMessageId") == anchor_id
                     and other.get("text") == snapshot.get("text")]
            if len(peers) != 1:
                continue
            candidates = [row for row in rows if row.get("role") == "assistant" and not row.get("tool_calls")
                          and not _deleted(row) and anchor["id"] < row["id"] < end
                          and visible_text(row.get("content")) == snapshot.get("text")]
            if len(candidates) != 1:
                continue
            key = self._native_key(session_id, candidates[0])
            account.store.database.execute("INSERT OR IGNORE INTO account_metadata(key,value) VALUES (?,?)", (key, snapshot["messageId"]))

    def _host_user_edits(self, account: Any, database: Any, session_id: str, rows: list[dict[str, Any]], presentations: dict[str, dict[str, Any]]) -> None:
        for message_id, snapshot in list(presentations.items()):
            if snapshot.get("sender") != "user":
                continue
            anchor = snapshot.get("anchorClientMessageId") or snapshot.get("clientMessageId")
            related = [row for row in rows if row.get("role") == "user" and row.get("platform_message_id") == anchor
                       and not row.get("_compressed_summary")]
            active = [row for row in related if row.get("active", 1)]
            archived = [row for row in related if _deleted(row)]
            if not active or not archived:
                continue
            current = max(active, key=lambda row: row["id"])
            previous = max(archived, key=lambda row: row["id"])
            proof = self._native_key(session_id, current)
            # 普通重写没有软归档，压缩归档为 compacted；仅认明确同身份替换。
            if (current["id"] <= previous["id"] or visible_text(current.get("content")) == visible_text(previous.get("content"))
                    or snapshot.get("nativeEditKey") == proof):
                continue
            if snapshot.get("batchId"):
                # 合并输入的编辑不能猜测如何拆分成原批次成员。
                continue
            text = visible_text(current.get("content"))
            parts = [{"type":"text", "text":text}] if text else []
            content = current.get("content")
            if isinstance(content, list) and any(isinstance(part, Mapping) and part.get("type") != "text" for part in content):
                parts.extend(part for part in snapshot["parts"] if part.get("type") != "text")
            stored = self._save(account, database, session_id, {**snapshot, "text":text, "parts":parts,
                "nativeEditKey":proof, "state":"CONFIRMED"})
            presentations[message_id] = stored
            # 沿用已分派的正文 upsert 事件；附件变化由带单条修订的历史响应体现。
            if all(part.get("type") == "text" for part in stored["parts"]):
                payload = {name:stored[name] for name in ("conversationId","messageId","sender","parts","text","timestamp")}
                payload["revision"] = stored["revision"]
                account.events.append("conversation.message.completed", message_id, payload)

    def read(self, account_id: str, conversation_id: str) -> list[dict[str, Any]]:
        account = self.core.open_gateway_account(account_id)
        try:
            binding = account.agent_sessions.lookup(conversation_id)
            if not binding or not binding.get("agentSessionId"):
                return []
            session_id = binding["agentSessionId"]
            database = self._database(session_id, binding.get("sessionKey"))
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

            presentations = self._presentations(database, account_id, conversation_id, rows)
            self._host_user_edits(account, database, session_id, rows, presentations)
            self._bind_replies(account, session_id, rows, presentations)
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
                    if candidate and candidate[0][len(prefix):] in presentations:
                        # 有展示记录却没有明确原生锚点时，不能靠旧摘要重新猜测身份。
                        candidate = None
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
                    # 快照撤回没有独立的恢复契约，不冒充可撤销的内容修订事件。
                    item.pop("contentRevision", None)
                    HistoryMedia(account).mark_message_deleted(item["messageId"],conversation_id)
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

    def media_message_deleted(self, account: Any, conversation_id: str, message_id: str) -> bool:
        """原件路由已有账号事务，核对宿主事实时不得另开同账号写事务。"""
        binding = account.agent_sessions.lookup(conversation_id)
        if not binding or not binding.get("agentSessionId"):
            return False
        session_id = binding["agentSessionId"]
        database = self._database(session_id, binding.get("sessionKey"))
        rows = self._history_rows(database, session_id)
        related = []
        for row in rows:
            if row.get("role") not in {"user", "assistant"} or row.get("_compressed_summary"):
                continue
            mapped = account.store.database.execute("SELECT value FROM account_metadata WHERE key=?", (self._native_key(session_id,row),)).fetchone()
            protocol = account.store.database.execute("SELECT message_id FROM messages WHERE conversation_id=? AND client_message_id=? AND sender=?",
                (conversation_id,row.get("platform_message_id"),row["role"])).fetchone()
            if (mapped and mapped[0] == message_id) or (protocol and protocol[0] == message_id):
                related.append(row)
        if related:
            return _deleted(max(related,key=lambda row:row["id"]))
        value = database.get_meta(self._presentation_key(account.account_id,conversation_id,message_id))
        snapshot = json.loads(value) if value else None
        if snapshot and snapshot.get("state") == "DELETED":
            return True
        anchor = snapshot.get("anchorClientMessageId") if snapshot else None
        anchors = [row for row in rows if row.get("role") == "user" and row.get("platform_message_id") == anchor
                   and anchor and not row.get("_compressed_summary")]
        return bool(anchors and _deleted(max(anchors,key=lambda row:row["id"])))

    def cleanup_account(self, account_id: str) -> None:
        account = self.core.open_gateway_account(account_id)
        try:
            databases = {}
            for binding in account.agent_sessions.list():
                if not binding.get("agentSessionId"):
                    continue
                database = self._database(binding["agentSessionId"], binding.get("sessionKey"))
                databases.setdefault(database, set()).update(self._session_chain(database, binding["agentSessionId"]))
        finally:
            account.close()
        # 先核对没有另一个账号绑定同一会话，避免异常旧绑定扩大删除范围。
        for other_id in self.core.list_gateway_account_ids():
            if other_id == account_id:
                continue
            other = self.core.open_gateway_account(other_id)
            try:
                for binding in other.agent_sessions.list():
                    if not binding.get("agentSessionId"):
                        continue
                    database = self._database(binding["agentSessionId"], binding.get("sessionKey"))
                    if database in databases and set(self._session_chain(database, binding["agentSessionId"])) & databases[database]:
                        raise RuntimeError("HOST_HISTORY_BINDING_CONFLICT")
            finally:
                other.close()
        with self._migration_lock:
            for database, sessions in databases.items():
                write = getattr(database, "_execute_write", None)
                if not callable(write):
                    raise RuntimeError("HOST_HISTORY_UNAVAILABLE")
                def clear_owned(conn):
                    # 回调已持宿主写锁，读取回退会再次取同一锁；必须复用当前连接。
                    prefix = self._presentation_prefix(account_id)
                    conn.execute("DELETE FROM state_meta WHERE substr(key,1,?)=?", (len(prefix), prefix))
                    for session_id in sessions:
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
