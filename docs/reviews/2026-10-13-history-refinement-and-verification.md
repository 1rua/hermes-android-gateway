# 历史恢复独立审查补修与验证

## 审查与最终实现

基线为已推送但未部署的 `a4a6acc855e2499ef11857a0f9ca438e7f424f63`；真实 Hermes SDK 固定为 `75b083e9399115b12513385252811f09c3503c90`。前一版本的 CI 通过后，独立审查捕获一项 P1 与四项 P2。本轮按用户最新指示由主 Agent 独立补修与复核，不再使用子 Agent。此前审查原始复现保留，不能把前一版 CI 当作最终部署许可。

1. 展示记录改用原生 `SessionDB.get_meta/set_meta/list_meta_prefix`，不再插入模型 `messages/session_meta`。真实 `archive_and_compact(tail_count=2)` 的模型尾部与消息计数保持不变；展示原稿、已发布提示及时间仍由宿主持有，Gateway 只保存身份、时间、修订和摘要索引。旧展示行在同一宿主写事务中完成迁移后才清除，更新原生计数；模型行与其他账号、储存归属保留。
2. 数据库归属优先使用绑定已保存的 `sessionKey` 交给原生 `SessionStore._db_for_key`，随后在该数据库遍历合法压缩链。旧父会话 ID 不再使 named profile 清理误落到 ambient 数据库。数据库或必需 API 缺失时明确失败，不创造宿主状态。
3. 原件的 metadata、grant、content 入口独立核对最新的原生消息或明确用户锚点的撤回事实。实际 HTTP 路由已有账号事务，核对使用同一个账号连接，只读访问宿主，避免另开同账号写事务产生锁冲突。旧授权与新授权都不能绕过撤回；授权仍单次、限账号/会话/设备/配对/修订和期限。
4. 取消 `record_visible` 在全历史按文字认领回答。只有明确本轮用户锚点、有界的原生回合以及唯一候选共同成立时才关联最终回复；没有归属证据的独立通知与旧同文回答保留各自身份和时间。锚点只在宿主展示元数据中保存，不加入闭合的事件 Schema。
5. 识别原生 `archive_dropped=True` 的同身份单条用户替换，更新 canonical 展示正文并递增单条 `contentRevision`；普通模型重写和压缩差异不会被当作用户编辑。聚合批次不能猜测如何把宿主改写后的合并文本拆分成原成员，继续保持原成员身份。本轮不宣称提供聚合输入的任意拆分编辑 API。

公开时间线及原身份查询可返回兼容的可选 `contentRevision`。该整数是宿主展示记录的单条版本，不是全页 `snapshotRevision` 摘要，也不是 Agent 投递状态修订。流式事件采用同一记录实际递增的版本，使重新读取历史后更晚增量继续可见。正文为纯文本的明确编辑沿用已分派的 `conversation.message.completed` upsert；附件结构变化由带单条版本的完整历史响应体现，不增加未分派事件、不改核心 Schema。

明确快照删除仍按稳定身份返回 `DELETED` 和空正文/parts，沿用客户端已有删除屏障；本轮没有扩展撤销快照删除的契约。模型 transcript 的空列表不能独立证明一个没有原生记录的通知已被撤回。本轮撤回回归补齐真实的原生消息/平台身份，再调用同一 SDK 软归档接口，保留删除及原件拒绝的严格断言；没有用人工空状态伪造撤回。

## 已实际执行

| 验证 | 结果 |
| --- | --- |
| 压缩尾部与同文身份回归 | 2 failed → 2 passed；[RED](evidence/2026-10-13-history-refinement/compaction-and-identity-red.txt)、[GREEN](evidence/2026-10-13-history-refinement/compaction-and-identity-green.txt) |
| 撤回后未读取时间线的三个原件入口 | 3 failed，修复后纳入原生完整矩阵；[RED](evidence/2026-10-13-history-refinement/assets-red.txt) |
| 真实 SDK、原生流式与 HTTP 夹具 | 32 passed，0 failed/0 skipped，16.66 秒；[完整结果](evidence/2026-10-13-history-refinement/native-matrix-final-2.txt) |
| 旧展示行可重试迁移 | 1 passed，模型行逐项保持、原时间与版本保持、重复读取不再迁移；[结果](evidence/2026-10-13-history-refinement/native-kv-migration.txt) |
| 独立插件完整测试 | 341 passed，0 failed/0 skipped，59.95 秒；[完整结果](evidence/2026-10-13-history-refinement/standalone-matrix-final-2.txt) |
| 共享协议向量与安装声明 | 66/66；manifest、入口与协议 2.1.0 通过；[向量结果](evidence/2026-10-13-history-refinement/conformance-final.txt) |

32 项矩阵与迁移专项共覆盖 22 项展示、1 项既有 Native 迁移、1 项 Native 增量消费者、9 项 HTTP 夹具。9 项夹具与独立完整集重叠，不能把两个矩阵简单相加当作独立用例数。所有输入为合成样本，使用新私有临时目录和 42 个已跟踪契约文件的逐项摘要相同副本；没有读取生产账号正文、提示或凭据，没有操作手机。

首次完整矩阵发现账号事务重入锁冲突、无 SDK 的会话绑定 double 误启用原生读取，实际记录为 Native 1 failed/31 passed、独立 2 failed/339 passed。已修复真实事务路径与 SDK 连接条件后得到上表通过结果。首次展示专项 20 passed/1 failed 来自旧撤回夹具只有展示行、没有真实原生消息；新版存储不再将展示行当模型消息，该夹具已补齐原生记录，保留严格撤回断言。这些中间结果均不是通过证据。

## 兼容、交付与限制

核心摘要仍为 `sha256:99309b87dec79f9c737b7d875a6e949ceffb6bb18b468d7ce98ca37d77dd3d50`，Protocol 2.1、账号、主密钥、配对、宿主绑定及附件加密格式不变。两处当前运行安装还未应用本轮历史代码。

已被旧版本释放的原图字节无法重建；旧短期正文已清空时按明确身份交给客户端尚存的发送记录恢复，不从模型描述猜测原文。26 MiB 上传、投递和原件保留此前已验证；旧历史 content 接口仍有 25 MiB 上限。本报告不把确定性处理器当作两处真实模型或手机验收，最终提交、CI、部署及匹配 APK 由交付记录单独列出。
