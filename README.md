# Open Android Intelligence Gateway for Hermes

原生 Gateway Protocol v2.1 平台插件：把 Open Android Intelligence 手机端接入
Hermes Agent 宿主，通过一条经过认证的本地通道承载对话、通知、短信、设备请求与
附件传输。

本轮插件版本为 `2.2.0`，协议版本保持 `2.1.0`，新增 `conversation-management-v1`。
配套 APK 需来自应用仓库提交
[`f0b3f1bd4f6f3301b820e38c9f3241bcac05cd10`](https://github.com/1rua/open-android-intelligence/commit/f0b3f1bd4f6f3301b820e38c9f3241bcac05cd10)
或使用同一契约的后续版本；本次核心 Schema 摘要已更新，升级时须同时更新插件与 APK。
完整历史分页、共享文件夹与排序、原生归档/恢复/删除均已通过固定 Hermes SDK 的原生回归测试。
Android 真机闭环需在实际部署中另行验收。
停止生成沿用 Hermes 原生硬中断及任务取消路径，并绑定当前生成，避免误停后继任务。
命令目录读取当前宿主注册表、别名、插件和技能，按 Android 平台过滤技能。

## 安装

```bash
hermes plugins install 1rua/hermes-gateway-plugin --enable
```

安装完成后，插件注册平台、管理入口、HTTP 路由与原生命令。初次配置时直接运行向导即可，向导会自动拉取对应锁定的协议契约：

```bash
hermes gateway setup          # 自动同步契约并向导式创建手机连接账号
hermes open-android-intelligence status
```

日常宿主启动时**不执行网络请求**，仅当本地契约未就绪时由 `setup` 向导或账号创建命令自动按版本拉取；也可随时通过 `hermes open-android-intelligence contract sync` 手工同步或更新契约。

## 更新

```bash
hermes plugins update open-android-intelligence-gateway
hermes open-android-intelligence contract sync
hermes gateway restart        # 更新后需重启进程才会加载新代码
```

随后安装上述配套 APK 并重新连接账号。旧版 APK 使用的核心摘要不同，不能与本版插件混用。

未指定 `--ref` 时安装的是仓库当前分支的最新提交。需要锁定到某个不可变版本时：

```bash
hermes plugins install 1rua/hermes-gateway-plugin --ref <40位提交SHA>
```

若旧安装锁定了提交，普通更新仍会保留该锁定。选取本仓库已发布且包含 `2.2.0` 的完整提交，显式替换后同步契约、重启：

```bash
hermes plugins install 1rua/hermes-gateway-plugin --force --ref <已发布提交的40位SHA> --enable
hermes open-android-intelligence contract sync
hermes gateway restart
```

## 协议契约

手机端与宿主协商时会比对协议 Schema 摘要，因此宿主必须持有与手机端**同一份**契约
文件。本仓库**不携带**契约副本，只在 `contract-pin.json` 中锁定它在应用仓库中的
地址与提交：

```json
{
  "repository": "https://github.com/1rua/open-android-intelligence.git",
  "revision": "f0b3f1bd4f6f3301b820e38c9f3241bcac05cd10"
}
```

`setup`、账号创建或显式 `contract sync` 按该提交把 `gateway-contract/` 取到本插件目录下
（只检出这一棵子树，不会拉取整个应用仓库）。宿主加载只读取已就绪的本地契约。
这样契约只有一份真源，既不会与手机端漂移，也不必手工
同步副本。

随时可查看与修复：

```bash
hermes open-android-intelligence contract status
hermes open-android-intelligence contract sync
```

契约的完整性以 `core-dispatched-schemas.json`、`schemas/envelope.schema.json` 与
`vectors/vector-set-1.0.0.schema.json` 三者为准：缺任何一个都会被判定为未就绪，而不会让
网关在协商时才暴露问题。

`status` 会如实报告契约来源（`pin` / `cached` / `env`）、锁定提交与目录；未就绪时
给出可操作的中文原因，不会静默降级。

### 离线与内网部署

已经持有契约的部署者可以直接指定目录，跳过拉取：

```bash
export OPEN_ANDROID_GATEWAY_CONTRACT_ROOT=/secure/path/gateway-contract
```

内网镜像可用 `OPEN_ANDROID_GATEWAY_CONTRACT_REPOSITORY` 覆盖仓库地址（提交号仍
由 pin 决定）。显式传入的契约目录优先级最高，其次是该环境变量，最后才是向上查找。

## 历史附件与默认本地保存

用户上传和 Agent 发出的原件在投递前进入宿主
`HERMES_HOME/history-media/open-android-intelligence/` 专属目录，按 Gateway 存储归属、
账号和会话隔离。原始临时发送文件退休后，已经采用的历史仍可读取，不延长 Gateway
附件暂存的期限。旧历史引用仍有原件时按需迁移并保留附件身份；旧字节已丢失时
返回 `REMOTE_UNAVAILABLE`，不会把缺失的原件显示为成功保存。

新 App 使用已签名查询参数 `format=stream-v1` 依次读取 metadata、申请单用途 grant、
流式读取 content。响应携带明确的格式、媒体类型、原始长度和摘要；大于旧 25 MiB
门槛的文件按 64 KiB 块读取。无格式参数保留有界 JSON/Base64 响应。

契约固定在 `f0b3f1bd4f6f3301b820e38c9f3241bcac05cd10`，核心摘要为
`sha256:032f580246eb4a214624bded717819b4d5a76191208207edc9e5a4607f5c4d23`。
旧响应表示兼容仅适用于同一核心摘要；只升级 App 或插件一端会被
`PROTOCOL_INCOMPATIBLE` 拒绝登录。

## 会话、生成与命令

协商到 `conversation-management-v1` 后，手机可分页读取当前 Gateway 账号完整历史，
包括已归档会话；共享文件夹、归属和自定义顺序由账号内的乐观版本控制管理，多设备共用。
只操作已有可信原生绑定的会话，不导入其它本地 Agent 聊天。

归档与恢复调用 Hermes 原生会话存储；删除覆盖该会话的压缩链、原生消息与展示 KV，
并移除原生路由。运行或排队中的会话返回 `CONVERSATION_BUSY`。
删除屏障阻止旧请求、历史与附件入口重新建立已删除会话。
原生操作已完成但回执写入失败时，持久 pending 屏障在重启后继续有效；必须用同一设备的
相同请求 ID、操作和参数重试，以恢复真实原生回执。结果未确认时返回 `OUTCOME_UNKNOWN`。
升级前创建且未带标题元数据的旧会话也可以删除。

手机直接调用 `POST /v2/conversations` 创建空会话，宿主为其分配独立的原生 Agent
session，不向旧会话发送 `/new`。同一 `clientConversationId` 的网络重试复用会话及
session；相同客户端身份携带不同创建参数返回 `IDEMPOTENCY_CONFLICT`。

`conversation.message.completed` 只表示一条助手消息完成，工具进度和最终答复可处于
同一轮生成。整轮由原生 `on_processing_complete` 收口所有未完成进度消息，再发送
带 `generationId` 的用户消息 `completed` / `failed` 状态；取消使用宿主的硬中断和
原生处理任务取消路径。运行中的生成只有确认其原生任务已经取消才返回 `CANCELLED`；
正常完成返回 `ALREADY_COMPLETED`，超时保留 `OUTCOME_UNKNOWN`。无法接入宿主取消桥接时
不声明 `generation-cancel-v1`，回调移除或断线后撤销该能力。

`GET /v2/commands` 每次从当前 Hermes 命令注册表、可用插件命令及启用的技能读取，
技能采用与 Telegram 相同的宿主发现来源，按 Android 的禁用配置过滤，不继承 Telegram
菜单的数量或名称截断限制。目录内容变化时 `catalogVersion` 自动变化；宿主发现出错时
请求明确失败，不回填固定命令。录音附件支持 `.m4a` / `audio/mp4`，按原始字节
进入宿主 `AUDIO` 消息，由宿主完成处理。

## 配置

在 `~/.hermes/.env` 中配置：

```bash
OPEN_ANDROID_GATEWAY_HOST=0.0.0.0     # 监听地址
OPEN_ANDROID_GATEWAY_PORT=8045        # 监听端口
# Hermes 不向插件暴露 Secret Store，主密钥文件位置可显式指定
OPEN_ANDROID_INTELLIGENCE_GATEWAY_MASTER_KEY_FILE=/secure/path/gateway-master-key
```

主密钥文件为 32 字节随机数、权限固定 `0600`，默认位于
`~/.open-android-intelligence/gateway-master-key`，在 Hermes 数据目录之外，不会随
数据库、备份或诊断一起导出。文件缺失、是符号链接、不属于当前用户或组/其他用户
可读时，网关**拒绝启动**并打印原因。

也可在 `~/.hermes/config.yaml` 中配置：

```yaml
gateway:
  platforms:
    open_android:
      enabled: true
      extra:
        port: 8085
        host: "0.0.0.0"
```

## 账号管理

宿主内使用原生命令：

```bash
hermes open-android-intelligence account list
hermes open-android-intelligence account create -u phone1 -p <密码> --confirm-local
hermes open-android-intelligence account delete phone1 --confirm-local
```

没有 Hermes 宿主时（例如离线预置），可用仓库内的等价 CLI，它与上面共用同一套管理
服务与主密钥来源：

```bash
python3 tools/hermes-account.py create phone1 --password <密码>
python3 tools/hermes-account.py status
python3 tools/hermes-account.py contract
```

## 开发与测试

```bash
python3 -m venv .venv
.venv/bin/pip install pytest jsonschema aiohttp cryptography \
  PyYAML==6.0.3 httpx==0.28.1 python-dotenv==1.2.4 psutil==7.2.2
```

测试需要真实协议契约（协商测试计算的是真实 Schema 摘要，不使用替身）。可设置
`OPEN_ANDROID_GATEWAY_CONTRACT_ROOT=/path/to/gateway-contract`，或先通过
`hermes open-android-intelligence contract sync` 按 pin 获取。standalone 与 native 必须分进程：

```bash
# 不加载 SDK，排除真正依赖原生宿主的测试组
env -u HERMES_SDK_ROOT -u PYTHONPATH .venv/bin/python -m pytest tests \
  --ignore=tests/test_native_history_complete.py \
  --ignore=tests/test_native_history_projection.py \
  --ignore=tests/test_native_streaming_delivery.py \
  --ignore=tests/test_native_turn_lifecycle.py \
  --ignore=tests/test_native_turn_review_boundaries.py \
  --ignore=tests/test_native_binding_http_failures.py \
  --ignore=tests/test_conversation_management.py \
  --ignore=tests/test_host_command_catalog.py \
  --ignore=tests/test_android_echo_fixture.py

# SDK 固定在 75b083e9399115b12513385252811f09c3503c90；安装 CI 中的原生依赖后运行
HERMES_SDK_ROOT=/path/to/hermes-agent PYTHONPATH=/path/to/hermes-agent \
  OAI_REQUIRE_NATIVE_HERMES=1 .venv/bin/python -m pytest \
  tests/test_native_history_complete.py tests/test_native_history_projection.py \
  tests/test_native_streaming_delivery.py tests/test_native_turn_lifecycle.py \
  tests/test_native_turn_review_boundaries.py tests/test_conversation_management.py \
  tests/test_host_command_catalog.py tests/test_native_binding_http_failures.py \
  tests/test_legacy_conversation_canonical.py tests/test_android_echo_fixture.py

.venv/bin/python scripts/check_plugin_manifest.py
```

CI 使用 Python 3.11，并为两个进程分别设置临时 `HERMES_HOME` 和
`HERMES_TEST_ISOLATION`。本地测试也应设置独立临时目录，避免访问日常 Hermes 数据。

提交前建议跑一次官方校验：

```bash
hermes plugins validate .
```

## 与应用仓库的版本关系

本仓库与应用仓库独立发版。以下两件事必须同步，否则手机端会在协商阶段收到
`PROTOCOL_INCOMPATIBLE`：

1. **`gateway-contract/schemas/*.schema.json` 变更** → 核心 Schema 摘要随之变化，
   必须把插件与手机端一起升级到同一版本。
2. **插件协议版本升级** → `contract-pin.json` 的 `revision` 必须指向包含该契约的
   提交。

插件在加载时会校验清单声明的协议版本与核心实现是否一致，不一致直接拒绝加载并
说明差异，因此跨仓升版不会静默漂移到协商阶段才暴露。

## 许可

MIT，与应用仓库一致。
