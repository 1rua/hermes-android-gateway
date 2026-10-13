# 历史恢复推送后 Standards 审查发现记录

固定差异为 `c80e3cf6c12c418a4c198d44f25a97cb5f50868d...a4a6acc855e2499ef11857a0f9ca438e7f424f63`，SDK 为 `75b083e9399115b12513385252811f09c3503c90`。

独立 Standards 子 Agent 已完成以下真实复现并向主 Agent 提交发现；其回合随后被平台额度/容量错误中断，未完成最终报告。本文由主 Agent按原消息与已保存探针整理，不伪称完整独立审查已经零问题。用户随后明确要求不使用子 Agent，补修由主 Agent承担。

- **P1：展示行污染真实压缩尾部。** `host_history.py` 原 `_presentations` 把物理尾部新增 `session_meta` 的 `active=0,compacted=0` 当作删除，覆盖仍活跃的回复，并撤销原件。实际 SDK `conversation_compression.py` 与 `micro_compaction.py` 均调用 `archive_and_compact(...,tail_count=...)`，原用例只覆盖默认零尾部。实际探针与失败日志见 [压缩复现](evidence/2026-10-13-history-post-push-standards/native-compaction-probe.txt)。
- **P2：named profile 压缩后清理使用错误数据库。** 原 `cleanup_account` 按旧父 `sessionId` 解析数据库；真实 reroute 后父 ID 没有当前 owner key，SDK 回退 ambient DB，清理返回成功但所属 profile 仍保留一条账号展示原文。此发现只证明新增展示清理漏清，不扩大成跨账号读取。[复现](evidence/2026-10-13-history-post-push-standards/native-profile-cleanup-probe.txt)

补修使用原生 KV、已有 `sessionKey` 所属数据库以及同库压缩链，见 [主 Agent 补修与验证](2026-10-13-history-refinement-and-verification.md)。两个原始探针的数据均为合成，没有读取或删除真实账号，也没有操作服务或手机。本报告不能代替最终提交的 CI 与部署验收。
