"""历史原件由宿主持有，临时投递目录及 Gateway 附件 TTL 不决定其生命周期。"""
from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import secrets
import shutil
import stat
import tempfile
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs

from .core import GatewayError, _jcs, _now, iso_millis

MAX_MEDIA_BYTES = 25 * 1024 * 1024
STREAM_CHUNK_BYTES = 64 * 1024
STREAM_FORMAT = "stream-v1"
FORMAT_HEADER = "X-Open-Android-Intelligence-Media-Format"
_PRIVATE_FIELDS = {"path", "sourcePath", "storageOwner", "origin", "deleted"}


def historical_media_query(query: str, operation: str) -> tuple[str | None, str | None]:
    """格式与授权标识均取自已签名 target，不接受空值、重复或额外参数。"""
    try:
        parsed = parse_qs(query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise GatewayError("SCHEMA_INVALID") from exc
    allowed = {"format", "grantId"} if operation == "content" else {"format"}
    if set(parsed) - allowed or any(len(values) != 1 or not values[0] for values in parsed.values()):
        raise GatewayError("SCHEMA_INVALID")
    format = parsed.get("format", [None])[0]
    if format is not None and format != STREAM_FORMAT:
        raise GatewayError("SCHEMA_INVALID")
    token = parsed.get("grantId", [None])[0]
    if operation == "content" and not token:
        raise GatewayError("SCHEMA_INVALID")
    return format, token


def _media_type(value: str) -> str:
    if not isinstance(value, str) or not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise GatewayError("SCHEMA_INVALID")
    # aiohttp 的 HTTP 头使用 Latin-1 编码，元数据必须在发送字节前完成检查。
    try:
        value.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise GatewayError("SCHEMA_INVALID") from exc
    return value


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class HistoryMediaStream:
    """拥有可关闭原件句柄的有界迭代器；每块核对授权、来源及最后的摘要。"""
    def __init__(self, stream, record, validate: Callable[[], None], expires_at, now=None):
        self._stream = stream
        self._record = dict(record)
        self._validate = validate
        self._deadline = time.monotonic() + (_now(expires_at) - _now(now)).total_seconds()
        self._identity = _identity(os.fstat(stream.fileno()))
        self._count = 0
        self._digest = hashlib.sha256()
        self.closed = False
        media_type = _media_type(record["mediaType"])
        self.headers = {FORMAT_HEADER: STREAM_FORMAT, "Content-Type": media_type,
            "Content-Length": str(record["sizeBytes"]),
            "Digest": "sha-256=" + base64.b64encode(bytes.fromhex(record["sha256"].removeprefix("sha256:"))).decode("ascii")}

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed:
            raise StopIteration
        try:
            if time.monotonic() >= self._deadline:
                raise GatewayError("AUTHENTICATION_FAILED")
            self._validate()
            if _identity(os.fstat(self._stream.fileno())) != self._identity:
                raise GatewayError("ATTACHMENT_READ_FAILED")
            path = Path(self._record["path"])
            if _identity(path.stat(follow_symlinks=False)) != self._identity:
                raise GatewayError("ATTACHMENT_READ_FAILED")
            data = self._stream.read(STREAM_CHUNK_BYTES)
            self._count += len(data)
            self._digest.update(data)
            if self._count > self._record["sizeBytes"]:
                raise GatewayError("ATTACHMENT_READ_FAILED")
            if self._count == self._record["sizeBytes"] or not data:
                if self._count != self._record["sizeBytes"] or "sha256:" + self._digest.hexdigest() != self._record["sha256"]:
                    raise GatewayError("ATTACHMENT_READ_FAILED")
                self.close()
            if not data:
                raise StopIteration
            return data
        except StopIteration:
            raise
        except BaseException:
            self.close()
            raise

    def close(self):
        if not self.closed:
            self.closed = True
            self._stream.close()


class HistoryMedia:
    def __init__(self, account):
        self.account = account
        self.store = account.store

    def _get(self, key):
        row = self.store.database.execute("SELECT value FROM account_metadata WHERE key=?", (key,)).fetchone()
        return self.store.open_json(row[0], key) if row else None

    def _put(self, key, value):
        self.store.database.execute("INSERT OR REPLACE INTO account_metadata(key,value) VALUES (?,?)", (key, self.store.seal_json(value, key)))

    @staticmethod
    def _host_home():
        return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).resolve()

    @classmethod
    def _account_media_root(cls, account_id, storage_owner):
        owner = hashlib.sha256(str(Path(storage_owner).resolve()).encode()).hexdigest()
        return cls._host_home() / "history-media" / "open-android-intelligence" / "owners" / owner / "accounts" / hashlib.sha256(account_id.encode()).hexdigest()

    @classmethod
    def _legacy_media_root(cls, account_id, storage_owner):
        owner = hashlib.sha256(str(Path(storage_owner).resolve()).encode()).hexdigest()
        return cls._host_home() / "cache" / "open-android-intelligence-history" / "owners" / owner / "accounts" / hashlib.sha256(account_id.encode()).hexdigest()

    @staticmethod
    def _retire_path(path):
        path = Path(path)
        if not path.exists():
            return
        root = Path(tempfile.gettempdir()) / "open-android-intelligence-trash" / "history-media"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        shutil.move(str(path), str(root / (uuid.uuid4().hex + "-" + path.name)))

    @classmethod
    def retire_account_media(cls, account_id, storage_owner):
        cls._retire_path(cls._account_media_root(account_id, storage_owner))
        cls._retire_path(cls._legacy_media_root(account_id, storage_owner))

    @staticmethod
    def _upload_part(record):
        return {"type":"attachment", **{name:record[name] for name in ("attachmentId", "filename", "mediaType", "sizeBytes", "sha256") if record.get(name) is not None}}

    def upload_part(self, conversation_id, message_id, attachment_id):
        record = self._get("history-media:" + attachment_id)
        if record:
            if record["conversationId"] != conversation_id:
                raise GatewayError("ATTACHMENT_NOT_FOUND")
            return self._upload_part(record)
        try:
            original = self.account.attachments.get_record(attachment_id)
        except GatewayError:
            return {"type":"attachment", "attachmentId":attachment_id}
        record = {name:original[name] for name in ("attachmentId", "filename", "mediaType", "sizeBytes")}
        record.update(conversationId=conversation_id, messageId=message_id,
            sha256="sha256:" + str(original["sha256"]).removeprefix("sha256:"), path=None)
        self._put("history-media:" + attachment_id, record)
        return self._upload_part(record)

    @staticmethod
    def _valid_bytes(path, size_bytes, sha256):
        try:
            descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size != int(size_bytes):
                    return False
                digest = hashlib.sha256()
                while data := stream.read(STREAM_CHUNK_BYTES):
                    digest.update(data)
                return _identity(before) == _identity(os.fstat(stream.fileno())) and "sha256:" + digest.hexdigest() == sha256
        except (OSError, ValueError, TypeError):
            return False

    def _adopt_record(self, record):
        """先校验并原子复制到专属宿主目录，再发布可恢复的原件路径。"""
        if record.get("deleted") or not record.get("path"):
            return record
        root = self._account_media_root(self.account.account_id, self.account.paths.root.parent)
        folder = root / hashlib.sha256(record["conversationId"].encode()).hexdigest()
        source = Path(record["path"])
        if source.is_relative_to(root):
            return record
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        for private in (root.parent, root, folder):
            private.chmod(0o700)
        suffix = Path(record["filename"]).suffix
        if len(suffix) > 12 or any(not char.isalnum() and char != "." for char in suffix):
            suffix = ".bin"
        destination = folder / (hashlib.sha256(record["attachmentId"].encode()).hexdigest() + suffix)
        partial = folder / (uuid.uuid4().hex + ".part")
        try:
            descriptor = os.open(str(source), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream, partial.open("xb") as output:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size != int(record["sizeBytes"]):
                    raise GatewayError("ATTACHMENT_READ_FAILED")
                partial.chmod(0o600)
                total, digest = 0, hashlib.sha256()
                while data := stream.read(STREAM_CHUNK_BYTES):
                    total += len(data)
                    if total > int(record["sizeBytes"]):
                        raise GatewayError("ATTACHMENT_READ_FAILED")
                    output.write(data)
                    digest.update(data)
                output.flush()
                os.fsync(output.fileno())
                if total != int(record["sizeBytes"]) or "sha256:" + digest.hexdigest() != record["sha256"] or _identity(before) != _identity(os.fstat(stream.fileno())):
                    raise GatewayError("ATTACHMENT_READ_FAILED")
            os.replace(partial, destination)
            descriptor = os.open(str(folder), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            record = dict(record, sourcePath=record.get("sourcePath", str(source)), path=str(destination))
            self._put("history-media:" + record["attachmentId"], record)
            origin = hashlib.sha256(_jcs([self.account.account_id, record["conversationId"], record["messageId"], str(Path(record["sourcePath"]).resolve())]).encode()).hexdigest()
            self._put("history-media-origin:" + origin, record["attachmentId"])
            return record
        except BaseException:
            self._retire_path(partial)
            raise

    def adopt_inbound(self, conversation_id, message_id, attachments, paths):
        if len(attachments) != len(paths):
            raise GatewayError("ATTACHMENT_READ_FAILED")
        parts = []
        for attachment, source in zip(attachments, paths):
            attachment_id = attachment["attachmentId"]
            previous = self._get("history-media:" + attachment_id)
            if previous and (previous["conversationId"] != conversation_id or previous.get("deleted")):
                raise GatewayError("ATTACHMENT_NOT_FOUND")
            digest = "sha256:" + str(attachment["sha256"]).removeprefix("sha256:")
            if previous and previous.get("path") and previous["sha256"] == digest and self._valid_bytes(previous["path"], previous["sizeBytes"], digest):
                parts.append(self._upload_part(self._adopt_record(previous)))
                continue
            record = {name:attachment[name] for name in ("attachmentId", "filename", "mediaType", "sizeBytes")}
            record.update(conversationId=conversation_id, messageId=message_id, sha256=digest, path=str(source))
            parts.append(self._upload_part(self._adopt_record(record)))
        return parts

    def mark_message_deleted(self, message_id, conversation_id=None):
        for key, value in self.store.database.execute("SELECT key,value FROM account_metadata WHERE substr(key,1,14)='history-media:'").fetchall():
            record = self.store.open_json(value, key)
            if record.get("messageId") == message_id and (conversation_id is None or record.get("conversationId") == conversation_id) and not record.get("deleted"):
                self._put(key, dict(record, deleted=True))

    def reply_parts(self, message_id):
        prefix = f"history-media-reply:{message_id}:"
        rows = self.store.database.execute("SELECT key,value FROM account_metadata WHERE substr(key,1,?)=? ORDER BY rowid", (len(prefix), prefix)).fetchall()
        return [self.store.open_json(value, key) for key, value in rows]

    def register(self, conversation_id, message_id, path, media_type=None):
        origin = hashlib.sha256(_jcs([self.account.account_id, conversation_id, message_id, str(Path(path).resolve())]).encode()).hexdigest()
        origin_key = "history-media-origin:" + origin
        previous_id = self._get(origin_key)
        if previous_id:
            previous = self._record(conversation_id, previous_id)
            if previous.get("deleted"):
                raise GatewayError("ATTACHMENT_NOT_FOUND")
            if not Path(path).exists() and previous.get("path") and self._valid_bytes(previous["path"], previous["sizeBytes"], previous["sha256"]):
                return self._upload_part(self._adopt_record(previous))
        try:
            from gateway.platforms.base import validate_media_delivery_path
            safe_path = validate_media_delivery_path(str(path))
            if safe_path is None:
                raise GatewayError("ATTACHMENT_NOT_FOUND")
            path = safe_path
        except ImportError:
            file = Path(path).resolve()
            if not any(file.is_relative_to(self._host_home() / folder) for folder in ("cache", "outputs")):
                raise GatewayError("ATTACHMENT_NOT_FOUND")
        try:
            file = Path(path).resolve(strict=True)
            descriptor = os.open(str(file), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise GatewayError("ATTACHMENT_NOT_FOUND")
                digest = hashlib.sha256()
                while data := stream.read(STREAM_CHUNK_BYTES):
                    digest.update(data)
                if _identity(info) != _identity(os.fstat(stream.fileno())):
                    raise GatewayError("ATTACHMENT_READ_FAILED")
                digest = digest.hexdigest()
            media_id = "media_" + hashlib.sha256(_jcs([self.account.account_id, conversation_id, message_id, str(file), digest]).encode()).hexdigest()[:48]
            previous = self._get("history-media:" + media_id)
            if previous and previous.get("deleted"):
                raise GatewayError("ATTACHMENT_NOT_FOUND")
            record = {"attachmentId":media_id, "conversationId":conversation_id, "messageId":message_id,
                "filename":file.name, "mediaType":_media_type(media_type or mimetypes.guess_type(file.name)[0] or "application/octet-stream"),
                "sizeBytes":info.st_size, "sha256":"sha256:" + digest, "path":str(file), "origin":origin}
            record = self._adopt_record(record)
            self._put(origin_key, media_id)
            return self._upload_part(record)
        except OSError as exc:
            raise GatewayError("ATTACHMENT_NOT_FOUND") from exc

    def _record(self, conversation_id, media_id):
        row = self._get("history-media:" + media_id)
        if not row or row["conversationId"] != conversation_id:
            raise GatewayError("ATTACHMENT_NOT_FOUND")
        self.account.conversations.get(conversation_id)
        return row

    def _refresh_host_state(self, conversation_id, media_id):
        reader = self.account.conversations.history_reader
        port = getattr(reader, "__self__", None)
        verify = getattr(port, "media_message_deleted", None)
        if callable(verify):
            record = self._record(conversation_id, media_id)
            if verify(self.account, conversation_id, record["messageId"]):
                self.mark_message_deleted(record["messageId"], conversation_id)

    def metadata(self, conversation_id, media_id, format=None):
        self._format(format)
        self._record(conversation_id, media_id)
        self._refresh_host_state(conversation_id, media_id)
        row = self._record(conversation_id, media_id)
        available = not row.get("deleted") and row.get("path") and (format == STREAM_FORMAT or row["sizeBytes"] <= MAX_MEDIA_BYTES)
        if available:
            available = self._valid_bytes(row["path"], row["sizeBytes"], row["sha256"])
            if available:
                try:
                    row = self._adopt_record(row)
                except (OSError, GatewayError):
                    available = False
        return {key:value for key,value in row.items() if key not in _PRIVATE_FIELDS} | {
            "remoteAvailable":bool(available), "status":"AVAILABLE" if available else "REMOTE_UNAVAILABLE",
            "estimatedLocalBytes":row["sizeBytes"] + 512}

    @staticmethod
    def _format(format):
        if format is not None and format != STREAM_FORMAT:
            raise GatewayError("SCHEMA_INVALID")

    def grant(self, conversation_id, media_id, context, now=None, format=None):
        metadata = self.metadata(conversation_id, media_id, format=format)
        if not metadata["remoteAvailable"]:
            raise GatewayError("ATTACHMENT_NOT_FOUND")
        token = secrets.token_urlsafe(32)
        expires = iso_millis(_now(now) + timedelta(seconds=120))
        self._put("history-media-grant:" + hashlib.sha256(token.encode()).hexdigest(), {
            "attachmentId":media_id, "accountId":self.account.account_id, "conversationId":conversation_id,
            "deviceId":context["deviceId"], "pairingGeneration":context["pairingGeneration"],
            "grantRevision":context["grantRevision"], "expiresAt":expires, "format":format})
        return {"grantId":token, "expiresAt":expires, "metadata":metadata}

    def _consume_grant(self, conversation_id, media_id, token, context, now, format):
        key = "history-media-grant:" + hashlib.sha256(token.encode()).hexdigest()
        expected = {"attachmentId":media_id, "conversationId":conversation_id, "deviceId":context["deviceId"],
            "pairingGeneration":context["pairingGeneration"], "grantRevision":context["grantRevision"], "format":format}
        with self.store.transaction():
            saved = self._get(key)
            if not saved or _now(saved["expiresAt"]) <= _now(now) or saved.get("accountId", self.account.account_id) != self.account.account_id or any(saved.get(key) != value for key,value in expected.items()):
                raise GatewayError("AUTHENTICATION_FAILED")
            self.store.database.execute("DELETE FROM account_metadata WHERE key=?", (key,))
            self._refresh_host_state(conversation_id, media_id)
            row = self._record(conversation_id, media_id)
            if not row.get("path") or row.get("deleted"):
                raise GatewayError("ATTACHMENT_NOT_FOUND")
            row = self._adopt_record(row)
            return row, saved["expiresAt"]

    def content(self, conversation_id, media_id, token, context, now=None):
        row, _expires = self._consume_grant(conversation_id, media_id, token, context, now, None)
        if row["sizeBytes"] > MAX_MEDIA_BYTES:
            raise GatewayError("ATTACHMENT_NOT_FOUND")
        try:
            descriptor = os.open(row["path"], os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                data = stream.read(MAX_MEDIA_BYTES + 1)
        except OSError as exc:
            raise GatewayError("ATTACHMENT_NOT_FOUND") from exc
        if len(data) != row["sizeBytes"] or "sha256:" + hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise GatewayError("ATTACHMENT_NOT_FOUND")
        return data, _media_type(row["mediaType"])

    def content_stream(self, conversation_id, media_id, token, context, now=None, validate=None):
        row, expires = self._consume_grant(conversation_id, media_id, token, context, now, STREAM_FORMAT)
        if not self._valid_bytes(row["path"], row["sizeBytes"], row["sha256"]):
            raise GatewayError("ATTACHMENT_NOT_FOUND")
        try:
            descriptor = os.open(row["path"], os.O_RDONLY | os.O_NOFOLLOW)
            stream = os.fdopen(descriptor, "rb")
        except OSError as exc:
            raise GatewayError("ATTACHMENT_NOT_FOUND") from exc
        try:
            def validate_local_record():
                self._refresh_host_state(conversation_id, media_id)
                current = self._record(conversation_id, media_id)
                if current.get("deleted") or any(current.get(name) != row.get(name) for name in ("path","sizeBytes","sha256","mediaType")):
                    raise GatewayError("ATTACHMENT_NOT_FOUND")
            return HistoryMediaStream(stream, row, validate or validate_local_record, expires, now=now)
        except BaseException:
            stream.close()
            raise
