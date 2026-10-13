# 原生历史修复推送后独立规格审查

审查固定差异 `c80e3cf6c12c418a4c198d44f25a97cb5f50868d...a4a6acc855e2499ef11857a0f9ca438e7f424f63`；原生 SDK 为 `75b083e9399115b12513385252811f09c3503c90`。本报告只包含 Spec 轴，不修改实现。

依据主仓模块化架构 §3.2/7.3、ADR 0044/0045/0051、对话规格 §6.1/17.6/17.7，以及用户“重新打开后原文、原图和已发布提示应保持”的要求。模型重放确实过滤展示侧记录，原稿按成员、会话及账号捕获；协议 Schema 未改，旧原件已丢失的恢复边界说明诚实。

## 可行动发现

1. **P2：媒体删除判定依赖先读取时间线。** `host_history.py:320–322` 才标记媒体删除；`history_media.py:128–151` 的 metadata/grant/content 不主动核对宿主。宿主明确撤回后、首次时间线读取前，原件仍为 AVAILABLE，旧 grant 下载成功且新 grant 仍可签发。与 §17.7“远端已删除…显示 REMOTE_UNAVAILABLE”不符。应在三个媒体入口核对当前宿主删除事实。[复现](evidence/2026-10-13-history-post-push-spec/native-deletion-before-read.json)

2. **P2：同文增量错误认领旧原生回答。** `host_history.py:194–204` 在整个历史中按正文认领未映射 assistant 行。新增量先发布且正文恰好等于旧回答时，旧行被绑定到新消息身份，原始时间被替换；真正新原生行随后取得另一个身份。违反明确消息身份及原始时间保持要求。应使用本轮原生身份、明确锚点和有界新行，正文不足以证明归属。[复现](evidence/2026-10-13-history-post-push-spec/equal-reply-identity.json)

3. **P2：同身份的明确宿主替换仍被旧展示原稿覆盖。** `host_history.py:289–291/312–319` 无条件优先旧快照。使用宿主明确的软归档替换模式，旧 user 已 inactive/非 compacted、新 active user 保持同 platform_message_id，返回内容仍为旧原稿。ADR 0044 要求“本地旧副本不得覆盖远端事实”。应识别明确编辑或撤回/新发布事实；不得从一般模型加工的正文差异推断编辑。[API 层复现](evidence/2026-10-13-history-post-push-spec/native-explicit-edit.json)

第三项的实际生产边界：SDK `tui_gateway/methods_prompt.py:412–414` 在明确确认的 rewind/edit/regenerate 后调用 `replace_messages(..., archive_dropped=True)`；普通 `gateway/session_transcript.py:507–509` 不传此标记，原位压缩为 `compacted=1`。当前复现保持相同平台身份；TUI 切断旧回合后重新提交未必保持该身份，不能据此声称所有生产编辑均触发本问题。

## 独立验证及范围

三个 CLI 探针均使用新建临时目录、合成账号与实际 SessionDB；没有读取真实账号或操作运行服务。独立原生回归 **14 passed、1 deselected**，[日志](evidence/2026-10-13-history-post-push-spec/native-selected-independent.txt)；先前包含大附件专项的执行被审查者中断，保留原日志，不计通过。未运行 Gradle 或手机操作；两处真实模型与手机验收尚未证明。本次 Spec 轴共三项 P2，修复并独立复审后才能闭环。
