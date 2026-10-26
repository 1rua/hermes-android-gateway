# Hermes 原生 Session Adapter

本仓库从 `3.0.0` 起是 OAI Core 的可选 Hermes 会话入口。它保留原插件标识 `open-android-intelligence-gateway` 和旧平台别名以解析历史会话，不再启动独立 Android Gateway，不维护设备请求调度器，也不注册旧设备工具。

```text
Android App ↔ OAI Core ↔ Hermes Session Adapter ↔ Hermes
```

设备协议、安全认证、账号配对、能力授权和 Claim/Result/ACK 状态机的唯一实现位于 `open-android-intelligence-core` 的 `oai_core.gateway`。本仓库旧 Python 模块仅转发到 Core 或可选 `oai-hermes-session-adapter`；两套源码不再同步。旧发行版及完整源码仍可通过 Git 提交 `b64dda00a0de9ff1a95a375dcf92f62b5a1027b3` 恢复。

## 配套安装

需要 Hermes 原生宿主，以及同一批次的 `open-android-intelligence-core>=0.2,<0.3` 与 `oai-hermes-session-adapter>=0.2,<0.3`。Core 自身可独立安装和运行，不会拉取本仓库或 Hermes SDK。

CI 使用 `oai-core-pin.json` 中的不可变 OAI 提交检出 Core 和可选 Adapter。尚未发布包索引前，在对应 OAI 仓库提交构建两个 wheel，连同本仓库 wheel 一起安装到 Hermes 的 Python 环境；此插件运行时不会下载旧 Gateway 或外部源码。源码安装时，从共同父目录执行：

```bash
python -m pip install ./open-android-intelligence/oai-core ./open-android-intelligence/integrations/hermes-session-adapter ./hermes-android-gateway
hermes plugins install ./hermes-android-gateway
```

不要同时安装此入口与 OAI 仓库的 `integrations/hermes-session-adapter` 插件入口，两者注册同一组原生平台；选择一个即可。设备工具统一使用 OAI Core CLI/MCP/API，并提供独立 Core 账号凭据。

## 原账号切换

宿主必须提供以下环境配置，真实值保存在当前用户可读的 `0600` 环境文件或服务管理器中，不得嵌入共享启动脚本：

- `OAI_HERMES_GATEWAY_ROOT`：原 Gateway 的既有 `accounts` 目录。
- `OAI_HERMES_ACCOUNTS`：唯一且显式的 Hermes 账号 ID JSON 数组。
- `OAI_HERMES_SESSION_TOKEN`：至少 32 字符，仅认证 Core 内部会话桥。
- `OAI_HERMES_SESSION_PORT`：可选，默认 `8792`，只监听 `127.0.0.1`。
- `OPEN_ANDROID_INTELLIGENCE_GATEWAY_MASTER_KEY_FILE`：原账号库的既有主密钥，保留原路径、文件所有者及 `0600` 权限。

Core 远程 Agent 的 `accounts` 配置将 Core 账号一对一映射到 Hermes 账号。不同账号不能共享会话上下文。内部桥 Token 不能充当 Core 账号、Android 设备会话或授权凭据。原生 SessionStore、SessionDB、压缩链和会话 ID 继续使用原数据，不合并原历史账号库与 Core 设备数据库。

切换前停旧 Gateway，离线备份完整账号目录（包含 WAL/SHM）、配置和单独的主密钥，保留旧发行版。新适配器为每个允许账号持有 SQLite `EXCLUSIVE` 连接；旧进程占库时激活失败，新适配器运行期间竞争写入被拒绝。Core 设备存储与旧 Hermes 账号库使用不同目录。

验证旧会话历史、账号隔离、流式回复、创建、重命名、归档、删除、取消及终端标题同步后再切换客户端。回滚时先停新适配器释放独占连接，再恢复旧发行版和原配置；数据格式未改变。需要恢复快照时保留当前目录供人工复核，不覆盖唯一数据副本。

## 验证与范围

`tests/` 检查轻量入口、元数据、无设备实现和旧模块对同一 Core 类的转发。配套 OAI 测试使用固定 Hermes SDK `75b083e9399115b12513385252811f09c3503c90`，验证真实插件加载器、SessionStore/SessionDB、旧平台来源、历史与压缩链、跨账号拒绝及原生生命周期。

原设备协议向量已迁移到 Core 测试。`legacy/` 保留旧发行版测试、诊断和账号脚本供离线复核，不随新插件打包，也不代表新入口继续提供旧设备服务。旧说明保留在 `docs/` 的历史文件中，部署以本说明及配套 OAI 迁移文档为准。

自动化使用临时合成账号和消息处理器；真实模型服务、生产 Hermes 部署以及 Android 真机仍须另行验收，不以协议模拟或本地宿主测试代替。
