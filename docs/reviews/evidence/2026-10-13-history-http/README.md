# 历史原文、工具提示与图片回取的跨端证据

本目录验证用户报告的历史显示故障：图片发送并收到回复后，切换会话再返回，原始用户正文和图片不能被模型加工输入取代，已发布的工具提示必须保留。

## 真实边界与隔离

- Android 使用生产 `GatewayAuthClient`、`GatewayHttpClient`、`AttachmentUploader`、`ConversationClient` 和 `GatewayConversationRepository`，通过真实回环 HTTP 上传、发送、订阅和读取。
- 历史夹具明确使用 `--history-agent`，通过 `HERMES_SDK_ROOT` 读取 NousResearch/hermes-agent `75b083e9399115b12513385252811f09c3503c90` 的真实 `BasePlatformAdapter`、`SessionStore` 和 `SessionDB`。缺少明确 SDK 来源时启动失败，不跳过用例。
- 夹具进一步核对上述三个实际模块的源码文件必须位于明确指定的 SDK 根目录；只在该目录放置数据库文件，再让继承的 `PYTHONPATH` 从其它目录补入宿主代码，会返回 `HERMES_NATIVE_SOURCE_MISMATCH`。
- 在任何宿主模块导入前隔离 `HERMES_HOME`、`HERMES_TEST_ISOLATION`、主密钥、账号、监听地址和随机端口。图片是代码中固定的一像素合成 PNG，不使用用户文件。
- 确定性处理器仅代替外部模型：它向原生会话存入合成模型加工文本与真实格式的工具调用记录，经生产适配器发布工具提示和最终回复。没有实际调用外部模型或搜索服务。
- 提供 SDK 时，旧 echo/stream 模式也注入真实 `SessionStore` 并保存原生用户/最终消息。未提供 SDK 的旧独立模式只验证 Core 与传输，明确保持宿主历史未连接；该分支不能替代新历史用例。

## 差分结果

| 证据 | 结果 | 检查内容 |
|---|---|---|
| `python-native-history-red.txt` | 基线插件 `c80e3cf`，1 failed、1 passed | 未修复生产投影把合成模型描述及宿主引用显示成用户原文。通过项是缺少 SDK 的拒绝测试。 |
| `android-native-history-red.txt` | 1 test、1 failed | 生产 Android 客户端重新读取后出现模型前缀，并丢失原附件部分。夹具同时证明真实 SessionDB 已存模型输入、临时接收图片已释放。 |
| `python-fixture-native-green.txt` | 8 passed | 新历史回归、SDK 缺失拒绝及原有启动/批次/流式回归全部通过。 |
| `python-fixture-standalone-green.txt` | 7 passed | 未提供 SDK 的旧独立模式与 SDK 缺失拒绝通过；新历史正向用例由上行强制原生矩阵承担。 |
| `android-native-http-green.txt`、`android-native-http-green-counts.json` | 5 tests、0 failures/errors/skipped | 新历史回归，以及原有普通上传、批次、默认 WebSocket 和 SSE 流式回归通过。任务使用 `--rerun`、`--no-build-cache`，没有从测试缓存复用。 |
| `android-native-history-runtime.txt` | `nativeSessionDb=true`、`modelInputPersisted=true`、`released=true` | 对应 Android 正向回归的阶段证据，不记录正文或凭据。 |
| `native-sdk-dependency-closure.json` | 原生数据库/存储/适配器来源及隔离检查通过 | 干净 Python 3.11 虚拟环境，只为 SDK 增加 `PyYAML==6.0.3` 与 `httpx==0.28.1`，另安装插件声明依赖和夹具需要的 `aiohttp`/`pytest`。未安装完整 Agent、`openai`、`rich` 或 `python-dotenv`。 |
| `tested-source-sha256.json` | 逐文件摘要 | 记录测试时的客户端测试、夹具及插件生产实现，后续有新修订应重新核对。 |
| `python-sdk-source-red.txt`、`python-sdk-source-green.txt` | 1 failed → 1 passed | 新的 SDK 来源负向回归。初次失败没有产生应有的来源拒绝，随后还碰到受管文件系统的 `0777` 密钥权限拒绝；这段失败不用于证明 HTTP 历史修复。新增来源校验后取得正确拒绝。 |
| `python-fixture-native-final-green.txt` | 9 passed、0 skipped | 来源校验完成后重新运行全部夹具，用指定真实 SDK 完成正向历史、来源拒绝、旧启动/批次/流式回归。 |
| `python-fixture-standalone-final-green.txt` | 7 passed、2 deselected、0 skipped | 没有 SDK 的独立传输回归；明确排除原生历史正向与来源负向，两项均由上行强制 SDK 矩阵承担。 |
| `source-closure-after-forwarding.json` | 本轮源码与原生模块摘要 | 记录构建输入补齐后的工作树，含实际 SDK 提交和原生模块摘要。Android HTTP 5 项尚须对这组新输入重新执行，不把旧通过记录当作新运行结果。 |

新用例检查相同 `messageId`、`clientMessageId`、原始正文、附件 ID/文件名/媒体类型/长度、原工具提示及时间。图片处理临时源释放后，既有 `metadata → cache-grant → content` 安全入口仍返回同 ID 和 SHA-256 对应的原始字节；已消费的授权返回 `401 / AUTHENTICATION_FAILED`，其它会话访问返回既有 `400 / ATTACHMENT_NOT_FOUND`。

`android-history-boundary-expectation-adjustment.txt` 是中间调试记录：当时测试错误预期其它会话拒绝为 404，实际既有映射为 400。该记录不计入通过证据，最终测试按原稳定错误码验证，没有修改拒绝行为。

## 构建输入与执行环境

`conversation-data` 的测试任务显式传入插件、契约和 SDK 根。任务将插件 Python 实现/测试、清单和契约锁定文件，契约 JSON/实现，以及 SDK Python 源码和依赖声明作为具有相对路径敏感性的标准 Gradle 文件输入，同时记录插件与 SDK 的实际 Git `HEAD`。这样既能识别提交变化，也能识别同一提交上的未提交修复；SDK 输入排除 Git、虚拟环境、工具链、临时目录及内嵌依赖。

没有指定 SDK 时，解码等独立单元测试仍能配置；需要宿主的历史互通明确拒绝缺少来源。空的环境变量按未指定处理，不能解析成主仓根目录，也不能把主仓工具链误当成 SDK/契约输入。

本轮受限 sandbox 中两次小矩阵无有效输出，已精确终止，退出码 `143`，未计为通过；随后使用正常回环权限取得上表的 9 项与 7 项结果。受管项目目录会把新文件权限显示为 `0777`，真实密钥守卫按要求拒绝该存储位置。最终运行只把隔离夹具的小图片、密钥和数据库放到新建的私有 `/tmp` 根，证据仍写回两仓。没有修改生产权限守卫，也没有读取生产账号或发送生产消息。

## 可复现入口

先准备精确修订的插件源码与上述 SDK，并将 `HERMES_PLUGIN_ROOT`、`HERMES_SDK_ROOT`、`OPEN_ANDROID_GATEWAY_CONTRACT_ROOT` 指向对应目录。源路径由环境解析，不依赖本机固定安装位置。虚拟环境禁止继承系统包，安装插件及上述最小依赖后：

```bash
python -m pytest tests/test_android_echo_fixture.py -q
```

上述命令在插件根运行；新历史正向用例严格要求 `HERMES_SDK_ROOT`。Android 在 `apps/android` 运行：

```bash
./gradlew :conversation-data:testDebugUnitTest --rerun \
  --tests '*HermesHttpInteropTest' \
  --offline --no-daemon --no-build-cache --console=plain
```

基线差分使用 `c80e3cf` 的独立检出，只复制新夹具和测试，保留未修复的生产文件。证据覆盖协议、历史投影及受控图片读取；手机界面实际预览和部署后的外部 Agent 回合仍由实机验收证明。
