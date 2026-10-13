# 会话重新打开后的原文、图片与工具提示诊断

日期：2026-10-12。范围：独立 Hermes 插件的原生历史投影。此报告只包含源码检查、合成回归与原生 API 探针；未修改生产实现，未读取真实账号正文或模型提示，未操作 Gateway 服务或手机。

本文保留诊断阶段的基线结论。后续实现、迁移边界与最新验证见 [原生历史展示修复报告](2026-10-13-native-history-projection-bugfix.md)。

## 基线与实机反馈

- 实机已部署的插件程序提交：`8a883b907dd46704de2946ff0e4edec26ed92d1f`。
- 本诊断使用的插件源码：`c80e3cf6c12c418a4c198d44f25a97cb5f50868d`；与程序提交之间只有后续证据文档。
- Android 用户安装的本轮发布：`d5e1838`。
- 原生宿主：Hermes `75b083e9399115b12513385252811f09c3503c90`，使用真实 `SessionDB`。

用户已确认本机选择、上传、发送附件以及真实 Agent 获取附件成功。新问题的触发是：收到回复后切换到其他会话，再回到原会话。两张截图显示，切换前用户气泡含图片和原文，工具进度含正常文字；切换后同一气泡出现模型识图描述和宿主媒体引用，图片消失，工具位置出现空文本提示。

截图中的图片说明格式对应 Hermes 图片预处理生成的 `user` 输入内容。现有代码和此次合成验证证明了“模型加工输入作为用户历史被展示”；它们不足以证明真正 `role=system` 的完整系统提示被展示。当前插件已经排除 `system` 角色。截图中的命令、路径和工具用法只作为显示问题的证据，未被执行。

## 已执行的最小回归

测试位于 [test_native_history_projection.py](../../tests/test_native_history_projection.py)。它们用新建的临时原生 `SessionDB`、隔离 Gateway 账号、真实消息受理/状态操作和 `ConversationPort.list_messages` 公共接口，不替换历史投影实现。

| 用例 | 原实现的结果 | 对应实机症状 |
| --- | --- | --- |
| 原文是“请说明这张图片”，原生模型输入前面含两段合成加工说明 | `messageId` 和 `clientMessageId` 正确，但返回正文含加工说明，原文断言失败 | 内部识图说明和宿主引用混入用户气泡 |
| 创建并验证合成图片附件，受理消息后由原生历史读取 | 切换前包含原附件 ID、文件名、媒体类型、长度；切换后仅剩文本 | 用户图片消失 |
| 使用真实 `adapter.send` 发布工具进度，原生存工具调用与最终回答 | 工具进度原 ID 不在历史中 | 已经显示的工具提示在重开后丢失 |
| 原生 `assistant` 记录只有 `tool_calls`，正文为 `None` | 被投影为普通 assistant，`parts=[{"type":"text","text":""}]` | “此消息没有可显示文本” |

基线恢复后的重跑结果为 **4 failed，0 skipped，0.77 秒**。这是有意保留的 RED 证据，不表示修复已经通过。输出全部来自合成数据，见 [native-projection-recovery-red.txt](evidence/2026-10-12-history-projection/native-projection-recovery-red.txt)。

可移植的复现命令如下，路径由调用方提供。`HERMES_SITE_PACKAGES` 是同一已验证宿主环境的依赖目录；`HISTORY_PYTHON` 是装有测试依赖的 Python 3.11；隔离目录必须是新建或仅用于此次诊断的目录。

```bash
PYTHONPATH="$HERMES_SOURCE:$HERMES_SITE_PACKAGES" \
HERMES_HOME="$HISTORY_DIAGNOSIS_ROOT/hermes-home" \
HERMES_TEST_ISOLATION="$HISTORY_DIAGNOSIS_ROOT/hermes-home" \
OPEN_ANDROID_GATEWAY_CONTRACT_ROOT="$ANDROID_SOURCE" \
"$HISTORY_PYTHON" -B -m pytest tests/test_native_history_projection.py -q --tb=short
```

## 已定位的链路

### 用户原文被模型加工内容替换

1. `adapter.py:1866–1871` 读取已核验附件和原始用户正文，`1884–1898` 按原 `clientMessageId` 创建宿主消息事件。
2. 宿主 `gateway/run_inbound.py:1694–1701` 依次加工媒体输入，`1955–1961` 生成识图说明与媒体引用，`1979–1980` 把这些段落放在原正文前面。该内容有模型用途，不是用户编辑出的聊天正文。
3. 宿主 `gateway/run_turn.py:1469–1474` 的持久正文只去掉时间标签和特定 Discord 提示，未移除本段识图加工内容；宿主正常模型持久化保留它用于之后的 Agent 上下文。
4. 插件 `host_history.py:62–75` 用客户端身份找到原生记录，写入稳定 ID 绑定后立即清掉 Gateway 的短期原文。
5. `host_history.py:84` 直接读取原生 `content`，`115–118` 以同一消息身份返回给 App。原生模型内容因而被当作用户可见文本。

这不是身份匹配失败，而是匹配成功后选择了错误的正文来源。只按图片说明前缀做正则删除无法形成可靠修复：用户可以合法输入相同文本，媒体加工还有语音、文档等形式。

### 用户附件丢失且没有历史原件映射

- Gateway 消息的原附件 ID 仍存于 `messages.attachment_ids_json`，普通 `core.py:3419–3441` 读取会用它拼接附件元数据。
- 原生分支 `host_history.py:117` 却只拼接 `HistoryMedia.reply_parts(message_id)`；该方法读取的是回复媒体关联，没有用户上传媒体关联。因此附件身份和元数据丢失。
- 还存在更深的原件缺口：`adapter.py:1980–2035` 为宿主处理生成的文件位于插件 `inbound-media` 临时目录；`on_processing_complete` 的 `2119–2121` 会清理它们。
- 当前 `HistoryMedia.register` 只在 `adapter.send_document` 回复附件路径使用，没有为用户上传 ID 建立宿主历史媒体原件映射。单独在历史 `parts` 中补回原附件 ID，仍不能令历史内容接口找到图片。

因此需要在交付阶段建立宿主拥有的历史原件及身份映射。Gateway 暂存区仍应在确认后清理，长期原件应归宿主媒体路径，按账号与会话隔离；历史接口只返回媒体标识与可用状态，不返回文件系统路径。

### 工具进度丢失与空记录误展示

- 实时进度走真实 `adapter.send`／完成事件，可以在手机即时显示。
- 开启原生历史后，`core.py:3347–3361` 对这些消息只存文本摘要关联并把 `messages.text` 设为空。工具进度和命令提醒未必由宿主模型 transcript 保存相同的文本，摘要关联因而没有可匹配的原生记录。
- `host_history.py:80–82` 只区分 `user`／`assistant`，没有区分 `assistant.tool_calls` 与面向用户的完成回答。`None` 正文经 `visible_text` 变为空字符串，仍在 `115–118` 构造普通回复；工具结果角色被直接排除。
- 空 assistant 记录和已发布进度应各自处理。单独隐藏空字符串会消除空气泡，但无法找回已经发布的进度、命令提示和相关身份。

## 原生展示 API 的兼容方案

已核对宿主 `hermes_state_messages.py`：

- `293–323` 的 `SessionDB.append_message` 支持 `api_content`、`display_kind`、`display_metadata`。
- `913–935`／`942–997` 的 `get_messages` 会解码这些字段，重启后保持可读。
- `380–397` 的 `set_latest_matching_message_display_kind` 按角色、正文和最新 active 记录匹配，并整体写入元数据。它不适合在重复正文、空原文或多个并发来源中替代明确 ID 绑定。
- `756–792` 的 `set_message_api_content`／`set_user_message_content` 可以改变已知原生记录，但也改变 Agent 的重放材料。本次不建议通过修改模型原始历史完成展示修复。
- 宿主 `gateway/run.py:1248–1252` 明确跳过 `session_meta` 与 `system` 记录。`session_meta` 已是原生 transcript 的标准记录角色。

建议在原生 `SessionDB` 用 `role=session_meta`、`content=None`、命名空间 `display_kind`／`display_metadata` 保存版本化的展示侧记录。记录保存原 `messageId`／`clientMessageId`、账号／会话、角色、原正文、parts 和原时间，原生 `platform_message_id` 使用独立的展示身份，不能占用用户 `clientMessageId`，避免宿主 `has_platform_message_id` 把它当作已处理用户输入。读取时以明确身份将展示侧记录与原生模型记录合并，不将侧记录发送给模型。

该方向已经执行真实原生 API 探针：见 [native_presentation_api_probe.py](evidence/2026-10-12-history-projection/native_presentation_api_probe.py) 和 [native-presentation-api-green.txt](evidence/2026-10-12-history-projection/native-presentation-api-green.txt)。结果：原生元数据往返完整、关库重开保留、Gateway 模型重放前后完全相同且仍只有原来的两条模型消息。该 GREEN 只证明兼容接口可用，未应用到生产插件，不能作为上述四项修复通过的证据。

## 修复与迁移边界

1. 受理时仍写 Gateway 短期队列；宿主接管时，先可靠写入原生展示侧记录，再清理短期原文。重复交付与重读须使用原身份，不追加重复气泡。
2. 用户图片保留原 `attachmentId` 的别名关系、文件名、媒体类型、长度和摘要。宿主长期媒体记录保存受限路径，现有历史 metadata／grant／content 接口继续做账号、会话、设备和授权检查。宿主原件已不存在时应明确返回不可用，不能伪装成可预览。
3. 已发布工具进度、命令提醒和回复媒体都进入宿主展示记录。原生无正文的工具调用要保留结构或排除内部占位，不把原始工具返回 JSON 自动作为聊天文字公开。
4. 保留宿主模型 `content`、`api_content`、工具与推理记录，避免因界面修复改变模型上下文。保留账号、配对、主密钥和绑定。
5. 对仍有短期原文的旧消息可按原身份迁移，只有宿主保存成功后才清空短期副本。已清空原文且没有展示元数据的历史无法从当前 Gateway 无损恢复；Android 原始发送记录可通过稳定身份提供本机可靠原稿，不能把内部加工文本当原文，也不能通过字符串猜测伪造迁移完成。
6. 已经完成并清理的历史图片若没有本机原件或宿主历史原件，不能恢复其字节。后续发送应建立正确的宿主原件关联。旧文字、草稿、附件元数据与冻结提交均应保留。
7. 原生删除／rewind／压缩状态应优先于展示侧副本：侧记录不得复活明确删除的消息，也不能把 `compacted` 历史误判为用户删除。此边界尚未运行本次回归，实施时需覆盖。

## 未验证事项

此阶段未实现修复，四项症状回归均为 RED；未执行服务器生产 E2E、手机界面操作或真实模型请求。用户本机附件送达已确认，服务器附件、回复逐段显示以及新问题的最终手机验收仍需后续证据。完整 CI、APK、两处更新应在实施与本机最小验证通过之后继续。
