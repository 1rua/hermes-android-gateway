# 原生历史展示与原件保留修复

本轮修复会话重新打开后用户原文被模型加工内容替换、用户图片缺失、工具进度丢失或变为空回复的问题。插件源码基线为 `c80e3cf6c12c418a4c198d44f25a97cb5f50868d`，原生宿主源码为 `75b083e9399115b12513385252811f09c3503c90`。基线诊断见 [诊断报告](2026-10-12-history-projection-diagnosis.md)。本报告记录源码与隔离验证，不代表两处运行服务或手机已经应用本次历史修复。

## 用户反馈与原因

用户已确认本机附件上传及真实 Agent 获取成功。新问题出现在回复完成后切换会话，再切换回原会话：用户气泡的原图与正文被内部识图说明、宿主媒体引用替换，已显示的工具进度变为没有可显示文本。

宿主模型输入与聊天展示具有不同用途。原实现正确匹配了原生用户消息的身份，却直接把供模型使用的 `content` 当作用户原文；同时只查询回复媒体关联，遗漏用户上传的原附件。工具进度与命令提示未必存在相同的模型 transcript 文本，原生 `assistant.tool_calls` 的空正文又被构造为普通空回复。修复保留模型原始材料，并在宿主支持的展示元数据中保存原稿与已发布内容。

## 实现与数据边界

- `host_history.py` 使用原生 `SessionDB.append_message` 写入 `role=session_meta`，正文为不含原稿的稳定标识，`display_kind=oai_gateway_presentation_v1`，版本化 `display_metadata.oaiGatewayPresentation` 保存账号、储存归属、会话、消息及客户端身份、原文、附件元数据、原始时间、状态和修订。长期原稿归属宿主；Gateway 索引只保存身份、修订、时间和摘要。
- 用户回合在交给宿主前捕获，每个批次成员单独保存其原始正文、原消息身份与批次归属。已发布的工具进度、命令提示、流式编辑和回复媒体也保存展示记录。编辑通过宿主 `set_latest_matching_message_display_kind` 按 `session_meta` 与稳定标识更新，保持原始时间，不为每个片段追加一条快照。标识与内容摘要包含储存归属，防止相同旧身份来自不同储存位置时覆盖彼此。
- 读取依据明确消息身份合并展示记录与原生模型记录。原生内部工具结果不作为聊天正文公开；没有原进度的旧工具调用只说明真实工具名称。宿主明确标记的 `_compressed_summary` 不作为用户发送内容显示。`compacted` 归档仍是历史，真正 inactive/rewind 优先形成删除事实，并阻止已删除消息的原件下载。
- `history_media.py` 在入站文件仍存在时按块复制并核验长度、SHA-256，把原件交到宿主允许的 `cache` 根下，以储存归属、账号和会话隔离。原始 `attachmentId` 继续作为历史媒体别名，现有 metadata/grant/content 接口完成账号、会话、设备和授权校验；模型处理仍读取原入站文件，处理完成后的临时文件释放不会删除历史原件。
- 显式账号删除只清理此插件、此账号、此储存归属的展示侧记录，并安全移动对应新增媒体到临时复核目录。清理使用真实宿主写事务，可重试；保留模型 transcript、其他账号和其他储存归属的记录。普通退出登录不触发该清理。

实际模型重放通过宿主 `gateway.run._build_gateway_agent_history` 验证。该生产函数跳过 `session_meta/system`；测试直接导入并调用它，未复制或改写其逻辑。原生 `get_messages_as_conversation` 本身会返回元数据行，因此只检查数据库行数不能证明模型输入未受影响。旧迁移测试已改为检查真实 user/assistant 序列，额外展示行不被计作模型消息。

## 兼容与迁移

Gateway Protocol 2.1、核心 Schema、既有附件分块格式、账号资料、主密钥、配对及宿主会话绑定均沿用原边界。

仍有 Gateway 短期原文的旧记录，在宿主保存展示记录成功后才清空短期副本。已由旧版本清空且没有展示记录的 Gateway 用户消息只返回稳定远端/客户端身份和空正文、空 parts，允许 Android 原始发送记录按身份提供原稿；不猜测剥离模型前缀。无 Gateway 映射的旧宿主纯文本继续正常可读。

旧入站原件已经清理时只保留已知附件身份与元数据，如实表示不可用，不能恢复已不存在的图片字节。新发送建立宿主原件关联。注册与 Agent 投递不再受既有 25 MiB 历史下载限制阻挡；26 MiB 原件已经验证可投递并保留。历史 content 接口仍沿用旧内存读取及 JSON/base64 返回方式，超过 25 MiB 的原件在此接口显示 `REMOTE_UNAVAILABLE`，原件仍由宿主持有；本轮没有引入新的下载协议。

## 已执行验证

全部数据均为合成样本，使用新建的 Gateway 账号目录、原生数据库与隔离 `HERMES_HOME/HERMES_TEST_ISOLATION`。未读取真实账号正文、提示、凭据或操作手机与服务。

| 验证 | 实际结果与证据 |
| --- | --- |
| 基线四项原问题回归 | 4 failed，0 skipped；[RED](evidence/2026-10-12-history-projection/native-projection-recovery-red.txt) |
| 旧宿主纯文本兼容 | 先捕获被清空的失败，再验证恢复原文；[RED](evidence/2026-10-12-history-projection/native-legacy-text-red.txt)、[GREEN](evidence/2026-10-12-history-projection/native-legacy-text-green.txt) |
| 大原件注册 | 26 MiB 注册原来抛 `ATTACHMENT_TOO_LARGE`，修复后通过；[RED](evidence/2026-10-12-history-projection/native-large-original-registration-red.txt)、[GREEN](evidence/2026-10-12-history-projection/native-large-original-registration-green.txt) |
| 大原件真实宿主投递 | 原生 SessionStore 与平台适配器接收 26 MiB 文件并读取完整摘要；完成后临时入站文件释放，宿主原件仍存；[证据](evidence/2026-10-12-history-projection/native-large-inbound-delivery.txt) |
| 原生压缩 | 原稿保留，内部压缩摘要不混入展示；[RED](evidence/2026-10-12-history-projection/native-compaction-red.txt)、[GREEN](evidence/2026-10-12-history-projection/native-compaction-green.txt) |
| 相同身份的储存归属隔离 | 复制的合成旧绑定不会覆盖另一归属的展示；[RED](evidence/2026-10-12-history-projection/native-storage-owner-red.txt) 与最终最小集 |
| 实际模型重放、重启、批次、删除与媒体隔离 | 最终 14 项投影回归全部通过，另加既有原生迁移与媒体授权 2 项，共 **16 passed，0 skipped，10.30 秒**；[最终最小集](evidence/2026-10-12-history-projection/native-minimal-projection-final.txt) |

最小环境仅包含插件/测试依赖与真实 SDK 源码，不含宿主 venv；Python 3.11 使用 `-B -s`，避免字节码写入宿主及加载用户 site。实际 SDK replay 导入还需 `PyYAML==6.0.3`、`httpx==0.28.1`、`python-dotenv==1.2.4`、`psutil==7.2.2`。完整独立单元集与强制 SDK 历史/流式/HTTP 专项分别运行，最终结果为 **341 passed** 与 **25 passed**，两套均为 **0 failed、0 skipped**。原生专项包含 14 项展示回归、1 项旧历史迁移、1 项真实增量消费者和 9 项已冻结的独立 HTTP 夹具；9 项夹具同时出现在独立完整集，两个矩阵的计数不能直接相加当作互不重叠的用例数。

曾尝试把全部既有单元测试放进同一强制 SDK 矩阵，得到 119 failed/236 passed。早期旧 helper 没有真实平台注册前置条件，夹具未提供 SDK 根变量，后半系统 `/tmp` 16 GiB 耗尽导致大量数据库失败，故不构成实现失败或通过的结论。完整失败日志已安全移动到主仓临时复核目录，文档只保存 [诊断摘要及摘要值](evidence/2026-10-12-history-projection/native-minimal-full-failed-summary.json)。之后的有效验证使用主仓内全新临时目录，不清理其他文件。

可移植的最小验证命令如下；变量由调用者指向当前插件、真实契约、固定 SDK 与隔离依赖目录，`CASE_TEMP` 必须是此轮新建的位置。

```bash
TMPDIR="$TEST_TEMP_ROOT" \
HERMES_HOME="$TEST_TEMP_ROOT/hermes-home" \
HERMES_TEST_ISOLATION="$TEST_TEMP_ROOT/hermes-home" \
HERMES_SDK_ROOT="$HERMES_SOURCE" \
OAI_REQUIRE_NATIVE_HERMES=1 \
OPEN_ANDROID_GATEWAY_CONTRACT_ROOT="$GATEWAY_CONTRACT_SOURCE" \
PYTHONPATH="$TEST_DEPENDENCIES:$HERMES_SOURCE" \
"$PYTHON_311" -B -s -m pytest -q \
  tests/test_native_history_projection.py \
  tests/test_native_history_complete.py \
  tests/test_history_media_complete.py \
  --basetemp="$CASE_TEMP" --tb=short
```

## 完整矩阵与补修复

第一次在受限网络环境执行时，本地随机监听端口被拒绝，不能作为协议失败或通过的证据。随后取得回环验证权限，但主仓临时目录所在文件系统将文件权限固定显示为 `0777`，即使调用 `chmod(0600)` 仍显示 `0777`；主密钥读取器正确拒绝该权限。`gateway-contract` 工作目录还带有未追踪且断链的 `node_modules`，已有测试的 `copytree` 会触及这些产物。该轮实际结果是 **35 failed、305 passed、0 skipped**，见 [失败日志](evidence/2026-10-12-history-projection/standalone-final-valid-matrix.txt)，未提交为通过结果。

有效验证采用新的 `/tmp/oai-history-final-20261013-*` 合成目录；真实权限探针在 `/tmp` 为 `0600`。契约只按主仓 `git ls-files gateway-contract` 导出 **42 个已追踪文件，共 324522 字节**，逐文件 SHA-256 核对完全一致，未复制主仓根、工具链、暂存或 `node_modules`。此副本的 core digest 为 `sha256:99309b87dec79f9c737b7d875a6e949ceffb6bb18b468d7ce98ca37d77dd3d50`，与本轮主仓相同，见 [契约来源闭包](evidence/2026-10-12-history-projection/clean-contract-closure.json)。证据日志持续写入本插件仓库，不把系统临时文件作为唯一交付物。

无宿主的独立测试入口曾在 `connect` 时安装一个没有 `SessionStore` 的历史读取器，新捕获步骤因此阻断真实 Core 短期消息投递。已先在相同签名 HTTP 批次入口捕获 **1 failed**，再收紧连接条件，取得 **1 passed**：[RED](evidence/2026-10-12-history-projection/standalone-history-disconnected-red.txt)、[GREEN](evidence/2026-10-12-history-projection/standalone-history-disconnected-green.txt)。只有原生 `SessionStore` 接入后才安装宿主历史，支持宿主在连接后注入；独立入口仍走真实 Core 短期传输，不声称提供宿主长期历史。

这个连接条件不吞掉真正原生 API 的故障。通过真实 Hermes `BasePlatformAdapter` 的公开发送接口分别验证连接前、连接后注入不可用数据库，均返回 `HOST_HISTORY_UNAVAILABLE`，没有完成事件，见 [可重复 CLI 探针](evidence/2026-10-12-history-projection/native-history-source-unavailable-probe.py) 与 [执行结果](evidence/2026-10-12-history-projection/native-history-source-unavailable.txt)。

旧增量消费者专项未给适配器注入真实会话，也未通过入站消息建立绑定，先取得 [原生失败证据](evidence/2026-10-12-history-projection/native-streaming-context-red.txt)。修复的是测试前置条件：使用真实 `SessionStore`、真实平台注册和签名 HTTP 入站建立会话，再由真实 `GatewayStreamConsumer` 发布三段内容。专项 [通过](evidence/2026-10-12-history-projection/native-streaming-context-green.txt) 并保留生成完成前逐段可见、最终确认、重开后同一消息身份及原始时间的断言；没有放松生产 `HOST_HISTORY_UNAVAILABLE`。

| 最终矩阵 | 结果 |
| --- | --- |
| 独立插件完整集，仅排除三个强制原生文件，父进程不注入 SDK | **341 passed，0 failed，0 skipped，58.65 秒**；[完整日志](evidence/2026-10-12-history-projection/standalone-final-matrix-green.txt) |
| 固定 SDK 的三个原生文件与已冻结 HTTP 夹具 | **25 passed，0 failed，0 skipped，15.79 秒**；[完整日志](evidence/2026-10-12-history-projection/native-final-matrix-green.txt) |

最终矩阵的可移植命令如下。`GATEWAY_CONTRACT_SOURCE` 必须是已核对的契约目录本身，包含 `schemas/`，不得指向 Android 仓库根目录。`TEST_TEMP_ROOT` 使用支持 POSIX 权限的新测试位置，每个 `--basetemp` 均是此轮新建目录。

```bash
TMPDIR="$TEST_TEMP_ROOT" \
HERMES_HOME="$TEST_TEMP_ROOT/standalone-home" \
HERMES_TEST_ISOLATION="$TEST_TEMP_ROOT/standalone-home" \
HERMES_SDK_ROOT="$HERMES_SOURCE" \
OPEN_ANDROID_GATEWAY_CONTRACT_ROOT="$GATEWAY_CONTRACT_SOURCE" \
PYTHONPATH="$TEST_DEPENDENCIES" \
PYTHONDONTWRITEBYTECODE=1 \
"$PYTHON_311" -B -s -m pytest -q tests \
  --ignore=tests/test_native_history_complete.py \
  --ignore=tests/test_native_history_projection.py \
  --ignore=tests/test_native_streaming_delivery.py \
  --basetemp="$TEST_TEMP_ROOT/standalone-final" --tb=short

TMPDIR="$TEST_TEMP_ROOT" \
HERMES_HOME="$TEST_TEMP_ROOT/native-home" \
HERMES_TEST_ISOLATION="$TEST_TEMP_ROOT/native-home" \
HERMES_SDK_ROOT="$HERMES_SOURCE" \
OPEN_ANDROID_GATEWAY_CONTRACT_ROOT="$GATEWAY_CONTRACT_SOURCE" \
OAI_REQUIRE_NATIVE_HERMES=1 \
PYTHONPATH="$TEST_DEPENDENCIES:$HERMES_SOURCE" \
PYTHONDONTWRITEBYTECODE=1 \
"$PYTHON_311" -B -s -m pytest -q \
  tests/test_native_history_complete.py \
  tests/test_native_history_projection.py \
  tests/test_native_streaming_delivery.py \
  tests/test_android_echo_fixture.py \
  --basetemp="$TEST_TEMP_ROOT/native-final" --tb=short
```

这些验证的模型回答均为确定性合成内容；原生数据库、平台入口、增量消费者、HTTP 与授权流程是实际实现。宿主数据库中的展示修订用于版本化原稿；HTTP 的 `snapshotRevision` 为全页摘要，只用于分页一致性，不应当作单条消息的编辑序号。

## 后续交付证据

主 Agent 负责两仓精确提交、推送后的独立审查、CI、APK 与两处保留数据的部署。跨端 HTTP 与 Android 会话重开回归由独立夹具验证。最终手机需检查重新打开原对话后原文、原图预览和工具提示保持正确；服务器附件和逐段回复结果仍需用户反馈。本报告不把确定性测试 Agent 当作生产模型实机验收。
