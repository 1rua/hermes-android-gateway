"""History assets refer to host-owned originals, never the Gateway staging directory."""
from __future__ import annotations
import hashlib,mimetypes,os,secrets,shutil,stat,tempfile,uuid
from pathlib import Path
from datetime import timedelta
from .core import GatewayError,_jcs,_now,iso_millis

MAX_MEDIA_BYTES=25*1024*1024
class HistoryMedia:
    def __init__(self,account): self.account=account; self.store=account.store
    def _get(self,key):
        row=self.store.database.execute('SELECT value FROM account_metadata WHERE key=?',(key,)).fetchone()
        return self.store.open_json(row[0],key) if row else None
    def _put(self,key,value): self.store.database.execute('INSERT OR REPLACE INTO account_metadata(key,value) VALUES (?,?)',(key,self.store.seal_json(value,key)))
    @staticmethod
    def _account_media_root(account_id, storage_owner):
        home = Path(os.environ.get('HERMES_HOME', str(Path.home()/'.hermes'))).resolve()
        owner = hashlib.sha256(str(Path(storage_owner).resolve()).encode()).hexdigest()
        return home/'cache'/'open-android-intelligence-history'/'owners'/owner/'accounts'/hashlib.sha256(account_id.encode()).hexdigest()
    @staticmethod
    def _retire_path(path):
        if not path.exists(): return
        # 管理操作只移动本插件拥有的媒体，留给用户复核，不清理其他宿主文件。
        root = Path(tempfile.gettempdir())/'open-android-intelligence-trash'/'history-media'
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        shutil.move(str(path), str(root/(uuid.uuid4().hex+'-'+path.name)))
    @classmethod
    def retire_account_media(cls, account_id, storage_owner):
        cls._retire_path(cls._account_media_root(account_id, storage_owner))
    @staticmethod
    def _upload_part(record):
        return {'type':'attachment', **{name:record[name] for name in ('attachmentId','filename','mediaType','sizeBytes')}}
    def upload_part(self, conversation_id, message_id, attachment_id):
        record = self._get('history-media:'+attachment_id)
        if record:
            if record['conversationId'] != conversation_id: raise GatewayError('ATTACHMENT_NOT_FOUND')
            return self._upload_part(record)
        try:
            original = self.account.attachments.get_record(attachment_id)
        except GatewayError:
            return {'type':'attachment','attachmentId':attachment_id}
        # 升级前原件已清理时仍保留身份及元数据，不能声称可以恢复图片。
        record = {name:original[name] for name in ('attachmentId','filename','mediaType','sizeBytes')}
        record.update(conversationId=conversation_id,messageId=message_id,
                      sha256='sha256:'+str(original['sha256']).removeprefix('sha256:'),path=None)
        self._put('history-media:'+attachment_id,record)
        return self._upload_part(record)
    def adopt_inbound(self, conversation_id, message_id, attachments, paths):
        if len(attachments) != len(paths): raise GatewayError('ATTACHMENT_READ_FAILED')
        parts = []
        for attachment, source in zip(attachments, paths):
            attachment_id = attachment['attachmentId']
            previous = self._get('history-media:'+attachment_id)
            if previous and previous['conversationId'] != conversation_id:
                raise GatewayError('ATTACHMENT_NOT_FOUND')
            expected_digest = str(attachment['sha256']).removeprefix('sha256:')
            if previous and previous.get('path') and previous['sha256']=='sha256:'+expected_digest and self.metadata(conversation_id,attachment_id)['remoteAvailable']:
                parts.append(self._upload_part(previous))
                continue
            root = self._account_media_root(self.account.account_id, self.account.paths.root.parent)
            folder = root/hashlib.sha256(conversation_id.encode()).hexdigest()
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            for private in (root.parent,root,folder): private.chmod(0o700)
            suffix = Path(str(attachment['filename'])).suffix
            if len(suffix)>12 or any(not char.isalnum() and char!='.' for char in suffix): suffix='.bin'
            destination = folder/(hashlib.sha256(attachment_id.encode()).hexdigest()+suffix)
            partial = folder/(uuid.uuid4().hex+'.part')
            total = 0
            digest = hashlib.sha256()
            try:
                descriptor = os.open(str(source),os.O_RDONLY|os.O_NOFOLLOW)
                with os.fdopen(descriptor,'rb') as stream, partial.open('xb') as output:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode): raise GatewayError('ATTACHMENT_READ_FAILED')
                    partial.chmod(0o600)
                    while chunk := stream.read(self.account.attachments.stream_chunk_bytes):
                        output.write(chunk)
                        digest.update(chunk)
                        total += len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if total != int(attachment['sizeBytes']) or digest.hexdigest()!=expected_digest:
                    raise GatewayError('ATTACHMENT_READ_FAILED')
                os.replace(partial,destination)
                record = {name:attachment[name] for name in ('attachmentId','filename','mediaType','sizeBytes')}
                record.update(conversationId=conversation_id,messageId=message_id,
                              sha256='sha256:'+expected_digest,path=str(destination))
                self._put('history-media:'+attachment_id,record)
                parts.append(self._upload_part(record))
            except BaseException:
                self._retire_path(partial)
                raise
        return parts
    def mark_message_deleted(self, message_id):
        for row in self.store.database.execute("SELECT key,value FROM account_metadata WHERE substr(key,1,14)='history-media:'").fetchall():
            record=self.store.open_json(row[1],row[0])
            if record.get('messageId')==message_id and not record.get('deleted'):
                record['deleted']=True
                self._put(row[0],record)
    def reply_parts(self, message_id):
        prefix = f"history-media-reply:{message_id}:"
        rows = self.store.database.execute(
            "SELECT key,value FROM account_metadata WHERE substr(key,1,?)=? ORDER BY rowid",
            (len(prefix), prefix)).fetchall()
        return [self.store.open_json(row[1], row[0]) for row in rows]
    def register(self,conversation_id,message_id,path,media_type=None):
        try:
            from gateway.platforms.base import validate_media_delivery_path
            safe_path = validate_media_delivery_path(str(path))
            if safe_path is None: raise GatewayError('ATTACHMENT_NOT_FOUND')
            path = safe_path
        except ImportError:
            home=Path(os.environ.get('HERMES_HOME',str(Path.home()/'.hermes'))).resolve()
            file=Path(path).resolve()
            if not any(file.is_relative_to(home/folder) for folder in ('cache','outputs')): raise GatewayError('ATTACHMENT_NOT_FOUND')
        file=Path(path).resolve(strict=True)
        if not file.is_file(): raise GatewayError('ATTACHMENT_NOT_FOUND')
        with file.open('rb') as stream: digest=hashlib.file_digest(stream,'sha256').hexdigest()
        media_id='media_'+hashlib.sha256(_jcs([self.account.account_id,conversation_id,message_id,str(file),digest]).encode()).hexdigest()[:48]
        record={'attachmentId':media_id,'conversationId':conversation_id,'messageId':message_id,'filename':file.name,'mediaType':media_type or mimetypes.guess_type(file.name)[0] or 'application/octet-stream','sizeBytes':file.stat().st_size,'sha256':'sha256:'+digest,'path':str(file)}
        self._put('history-media:'+media_id,record)
        return {k:v for k,v in record.items() if k not in {'path','conversationId','messageId'}}|{'type':'attachment'}
    def _record(self,conversation_id,media_id):
        row=self._get('history-media:'+media_id)
        if not row or row['conversationId']!=conversation_id: raise GatewayError('ATTACHMENT_NOT_FOUND')
        self.account.conversations.get(conversation_id)
        return row
    def metadata(self,conversation_id,media_id):
        row=self._record(conversation_id,media_id)
        try:
            file=Path(row['path']) if row.get('path') else None
            # 旧历史下载接口仍受内存读取上限约束，但注册和 Agent 投递不按此上限拒绝原件。
            available=not row.get('deleted') and row['sizeBytes']<=MAX_MEDIA_BYTES and file is not None and file.is_file() and file.stat().st_size==row['sizeBytes']
            if available:
                with file.open('rb') as stream: available='sha256:'+hashlib.file_digest(stream,'sha256').hexdigest()==row['sha256']
        except OSError: available=False
        return {k:v for k,v in row.items() if k not in {'path','deleted'}}|{'remoteAvailable':bool(available),'status':'AVAILABLE' if available else 'REMOTE_UNAVAILABLE','estimatedLocalBytes':row['sizeBytes']+512}
    def grant(self,conversation_id,media_id,context,now=None):
        metadata=self.metadata(conversation_id,media_id)
        if not metadata['remoteAvailable']: raise GatewayError('ATTACHMENT_NOT_FOUND')
        token=secrets.token_urlsafe(32); expires=iso_millis(_now(now)+timedelta(seconds=120))
        self._put('history-media-grant:'+hashlib.sha256(token.encode()).hexdigest(),{'attachmentId':media_id,'conversationId':conversation_id,'deviceId':context['deviceId'],'pairingGeneration':context['pairingGeneration'],'grantRevision':context['grantRevision'],'expiresAt':expires})
        return {'grantId':token,'expiresAt':expires,'metadata':metadata}
    def content(self,conversation_id,media_id,token,context,now=None):
        key='history-media-grant:'+hashlib.sha256(token.encode()).hexdigest()
        with self.store.transaction():
            saved=self._get(key)
            if not saved or _now(saved['expiresAt'])<=_now(now) or any(saved[k]!=v for k,v in {'attachmentId':media_id,'conversationId':conversation_id,'deviceId':context['deviceId'],'pairingGeneration':context['pairingGeneration'],'grantRevision':context['grantRevision']}.items()): raise GatewayError('AUTHENTICATION_FAILED')
            self.store.database.execute('DELETE FROM account_metadata WHERE key=?',(key,))
            row=self._record(conversation_id,media_id)
            if not row.get('path') or row.get('deleted'): raise GatewayError('ATTACHMENT_NOT_FOUND')
            try:
                fd=os.open(row['path'],os.O_RDONLY|os.O_NOFOLLOW)
                with os.fdopen(fd,'rb') as stream: data=stream.read(MAX_MEDIA_BYTES+1)
            except OSError as exc: raise GatewayError('ATTACHMENT_NOT_FOUND') from exc
            if len(data)!=row['sizeBytes'] or 'sha256:'+hashlib.sha256(data).hexdigest()!=row['sha256']: raise GatewayError('ATTACHMENT_NOT_FOUND')
            return data,row['mediaType']
