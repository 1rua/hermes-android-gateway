# 原生展示清理锁重入补修

## 已确认原因

[插件诊断 CI 38391980084](https://github.com/1rua/hermes-gateway-plugin/actions/runs/38391980084) 在 5 分钟限时后明确失败。30 秒调用栈与旧 SQLite 隔离复现完全相同：`cleanup_account → _execute_write → clear_owned → list_meta_prefix → _read_ctx`。宿主写事务持有非重入锁，读取连接不可用时，`_read_ctx` 会回退到同一写锁，因此等待无法结束。前一版在本机正常读取池上通过，未覆盖这条合法回退路径。

具体证据保存在[真实 CI 原始失败日志](evidence/2026-10-13-history-native-lock/plugin-ci-76cff3e-failed.txt)和[SQLite 3.45.0 隔离复现](evidence/2026-10-13-history-native-lock/sqlite345-reproduction-red.txt)。后者仅在测试进程用独立 `pysqlite3` 对照旧 SQLite，不是生产依赖变更，也不能据此声称 GitHub 实际 SQLite 版本已被读取。

## 修复与回归

清理回调复用宿主传入的事务连接，按字面前缀删除当前存储归属与当前账号的插件展示 KV。前缀中的下划线不能变成 SQL 通配符，因此使用参数化 `substr` 比较；不另开读取连接，也不修改 SDK 的锁或降级存储契约。旧展示侧行清理与模型计数仍处于同一原生写事务。模型消息、其他账号、其他存储归属及相似但不匹配的命名空间保持。

新增真实 `SessionDB` 回归强制读取回退，在独立限时进程中执行清理，然后逐项比较原模型行、另一个账号的 KV 及相似命名空间。原提交 `76cff3efef302d6e162d1e0afa482d315098d110` 的干净隔离副本只加入该回归，在有效契约副本下确实超时失败；[原实现失败证据](evidence/2026-10-13-history-native-lock/writer-fallback-red.txt)。补修后该用例及原有 profile 归属清理一起纳入完整原生矩阵。

[完整原生矩阵](evidence/2026-10-13-history-native-lock/native-matrix-green.txt)实际 34 passed、0 failed/0 skipped，包含迁移、展示、读取回退、原生增量、SSE/WebSocket 与 HTTP 夹具。真实 SDK 固定为 `75b083e9399115b12513385252811f09c3503c90`；此次修复与测试的字节摘要保存在[源码清单](evidence/2026-10-13-history-native-lock/source-sha256.json)。

| 最终最小验证 | 实际结果 |
| --- | --- |
| 独立插件完整测试 | [341 passed、0 failed/0 skipped](evidence/2026-10-13-history-native-lock/standalone-matrix-green.txt) |
| 强制真实宿主、流式与 HTTP | [34 passed、0 failed/0 skipped](evidence/2026-10-13-history-native-lock/native-matrix-green.txt) |
| SQLite 3.45.0 对照下的历史与迁移 | [24 passed、0 failed/0 skipped](evidence/2026-10-13-history-native-lock/sqlite345-matrix-green.txt)，使用重新核对的有效契约副本 |
| 主仓当前共享协议向量 | [66 / 66](evidence/2026-10-13-history-native-lock/conformance-green.txt)，明确导入本次插件源码 |
| 安装声明、入口与协议 | [通过](evidence/2026-10-13-history-native-lock/manifest-green.txt) |

341 与 34 包含重叠 HTTP 夹具，不相加为独立用例数；旧 SQLite 对照也覆盖已有历史用例。本机没有执行全量 Gradle，Android 完整编译与检查仍交由 CI。

本轮曾沿用已失效的旧临时契约路径，产生本机原生 4 failed/30 passed、独立 7 failed/334 passed。这些是未通过结果，不计作验证成功。随后在仓库忽略的临时目录重建仅包含 42 个已跟踪契约文件的副本，逐项摘要与主仓相同，预检三项必需文件后取得上述通过证据。没有复制整仓、读取生产账号或减少失败断言。

此补修不改 Gateway Protocol、核心 Schema、附件加密格式、账号或配对。两处运行中的安装尚未更新，仍须最终 CI 通过后按原计划部署并交由用户手动验收。
