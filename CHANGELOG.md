# 3.0.0

- 将 Android 设备协议与安全状态机迁移至 OAI Core，历史 Python 设备模块转发到 Core 同一实现。
- 插件默认仅运行 Hermes 原生会话桥，不再启动独立 Gateway 或注册设备工具。
- 保留原账号目录、主密钥、旧平台别名和原生 SessionStore/SessionDB/压缩链，采用 SQLite 独占连接防止旧库双写。
- Core 账号与 Hermes 账号必须显式一对一映射；桥接 Token 与设备授权分离。
- 测试和 CI 分别验证轻量入口与固定 Core/SDK 的原生会话契约；部署及回滚步骤见 README。

# Changelog

## 2.2.0

- 将应用仓库 `f0b3f1bd4f6f3301b820e38c9f3241bcac05cd10` 的会话管理实现纳入独立插件，并锁定同一契约；需配套 APK 一同更新。协议版本仍为 `2.1.0`。
- 新增账号范围完整历史分页、共享文件夹与排序，以及 `conversation-management-v1` 能力协商。
- 归档、恢复和删除调用真实 Hermes SessionDB，删除覆盖压缩链、消息、展示 KV 和原生路由；保留无关会话。
- 持久化 pending 操作屏障与删除 tombstone；回执持久化失败、进程重启后，原设备以同一请求 ID 重试可恢复真实原生结果。兼容升级前无标题元数据的旧会话删除。
- 保留已有原生停止、实时命令目录、历史附件与账号隔离修复。
- 将 standalone 与真实 SDK 测试分进程运行，并将会话管理与回执错误回归加入原生矩阵。
