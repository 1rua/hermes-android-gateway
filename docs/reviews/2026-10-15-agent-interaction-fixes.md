# Hermes 会话与生成交互修复

本轮与 Android 同步使用契约提交 `4dd825ad3ae6ad20019ff38f6ff5c4f1a8708394`，
核心摘要 `sha256:59244afa522108614123e4d047a8ff51f6f2e2651e9b800bb8c63e5e6e82d320`。

取消功能此前只通过消息 handler 的 `__self__` 查找 Hermes runner。真实宿主的
`_standalone_scoped` 返回闭包，导致已有硬中断桥接始终被判定为不可用。现在优先
使用宿主注入的 runner，再解析 handler 实际捕获的 bound method；handler 重新
接入时刷新能力。取消使用已绑定的真实 session key，调用宿主硬中断/运行失效
入口，再取消原生处理任务；没有桥接时不声明能力。

助手的一条进度消息完成不再用于推断整轮结束。所有增量、完成及附件事件携带
所属 `generationId`，原生 processing-complete 回调完成本轮仍打开的全部进度
消息，再通过用户消息 `completed` / `failed` 状态结算整轮。已结束回合的迟到输出
受到限制；不带旧回合上下文的独立提醒仍然可发送。

直接 `POST /v2/conversations` 记录 session 绑定，并在返回前接入独立的宿主
session，不发送 `/new`。创建以 `clientConversationId` 保持跨 HTTP 请求幂等，
重复请求不产生第二个 session；修改创建参数返回冲突，重命名不改变原创建收据。

命令目录改为实时读取 Hermes 的命令注册表及配置开关、插件命令和启用技能，移除
固定 Android 命令清单。响应采用正式 Android catalog 字段，不再附加 `command`
别名，也不把标题伪装为参数提示。

音频透传原本保留附件媒体类型和扩展名。本轮增加真实、无语音内容的 AAC/MPEG-4
静音样本，验证 Android 的 `.m4a` / `audio/mp4` 从签名上传、提交到原生 AUDIO
事件保持字节完全一致，并在处理完成后移除私有临时缓存。

验证记录：

| 范围 | 结果 |
| --- | --- |
| 全部独立测试，显式提供原生 SDK 给子进程夹具 | 372 passed，28 个原生专用测试 skipped |
| 最终定向 workflow、dispatch、消息身份及事件流 | 27 passed |
| 固定 SDK 的原生历史、流、批次、echo、生命周期与 AAC 矩阵 | 42 passed |
| 最新上游 SDK 的新增生命周期与 AAC 用例 | 5 passed |
| manifest、入口及 `git diff --check` | 通过 |

固定 SDK 为 `75b083e9399115b12513385252811f09c3503c90`；本轮获取的最新
上游为 `b51e8b850494e0a95a4b410c3d9b9a26df9ae261`。原生矩阵强制设置
`OAI_REQUIRE_NATIVE_HERMES=1`，使用真实 `BasePlatformAdapter`、原生 session
数据库、宿主 profile wrapper 和实际异步处理/取消任务；只替代外部模型边界。

完整日志及最终源码摘要保存在
[evidence/2026-10-15-agent-interaction](evidence/2026-10-15-agent-interaction)。
中间全量运行因执行服务连接中断而未形成结果；首次原生矩阵最后一个子进程读取到
编辑中的暂态 helper，报缺少 import，该结果没有计为通过。修正后冻结代码重跑，
得到上述完整原生结果。

部署需先发布匹配的契约与插件提交，同步更新 App、更新插件并重启 Hermes。
本轮未操作线上服务，也未运行真实模型推理或真实手机录音验收。
