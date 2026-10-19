# 会话管理同步与独立插件验证

将应用仓库 `f0b3f1bd4f6f3301b820e38c9f3241bcac05cd10` 的
`gateway-contract/patches/conversation-management/hermes.patch` 原样纳入插件。
基线 `main` 为 `8f7de532f096365d319250a728e1119ca88aad4c`，其 tree 与补丁原基线
`3b113cadc0526f0475233e7c41790d4a8fdd9fc7` 均为
`148815118d3f7ff81c953b80e9a53fc9ccc544aa`。`git apply --check` 通过后才应用，保留已有停止生成与实时命令目录实现。

插件版本统一为 `2.2.0`（`pyproject.toml`、`plugin.yaml`、Python 清单），协议版本仍为
`2.1.0`。`contract-pin.json` 固定上述应用提交；不提交第二份契约。
实际核心摘要为 `sha256:032f580246eb4a214624bded717819b4d5a76191208207edc9e5a4607f5c4d23`，需与配套 APK 同时升级。

## 结果

| 环境 | standalone | native | skip |
| --- | ---: | ---: | ---: |
| Python 3.11.17 / SQLite 3.53.1 | 407 passed (99.60s) | 92 passed (41.71s) | 0 |
| Python 3.12.14 / SQLite 3.53.1 | 407 passed (89.23s) | 92 passed (36.37s) | 0 |

原生 SDK 固定 `75b083e9399115b12513385252811f09c3503c90`，无需模型或供应商调用。
native 矩阵包含会话管理、压缩链归档/删除、升级后外部删除、签名 HTTP 删除屏障，以及原生操作完成后回执写入失败/重启/同请求恢复。
standalone 显式清除 SDK 环境，排除所有真正依赖宿主的测试组和 Android echo fixture；原生组在另一个进程执行，防止测试替身污染 SDK。

按 pytest 收集到的完整 node ID 比较：全部 473 项 = standalone 407 项 ∪ native 92 项，
无缺失、无额外项。交集 26 项全部来自 `test_legacy_conversation_canonical.py`，刻意在两种宿主环境重复验证。
会话管理、Android echo、命令目录、原生绑定、历史/流传输和生成生命周期这九个宿主测试文件均在 native 组执行；独立进程划分没有漏掉用例。

已通过原有 `resolve_contract_root`/`materialised_contract_root` 同步机制验证：以本地主仓 Git 镜像读取完整固定提交，冷启动 `unavailable` → 显式同步 `pin` → 缓存 `cached`；56 个契约文件与该提交逐字节一致，`.pinned-revision` 与 pin 相同。
`scripts/check_plugin_manifest.py` 通过，并新增三个插件版本声明一致性检查。
固定 SDK 的官方 `validate_plugin_dir` 检查全部通过；其唯一扫描 caution 来自基线已跟踪的历史 CI 日志 `plugin-ci-76cff3e-failed.txt` 中不可见字符。

Python 3.11 构建 sdist 与 wheel 成功。包元数据版本 `2.2.0`、Python 下限 `>=3.11`、依赖声明均正确；wheel 中六个本轮相关运行时源码与工作树逐字节一致，包含新 `conversation_management.py`。独立目录安装 wheel 后的导入、清单协议校验、新模块加载和契约摘要检查通过。

## 本地复现命令

本次实际 Python 3.11 为 `/workspace/gateway-sync/.venv-hermes311/bin/python`，
原交接路径 `/workspace/toolchains/oai-python/bin/python` 实际是 Python 3.12.14。
以下命令从插件仓根目录运行，两个进程使用独立 HOME；TMPDIR 位于磁盘。

```bash
env -u HERMES_SDK_ROOT -u PYTHONPATH \
  HOME=/workspace/gateway-sync/home/hermes311-standalone \
  HERMES_HOME=/workspace/gateway-sync/home/hermes311-standalone/hermes \
  HERMES_TEST_ISOLATION=/workspace/gateway-sync/home/hermes311-standalone/hermes \
  TMPDIR=/workspace/gateway-sync/tmp/hermes \
  OPEN_ANDROID_GATEWAY_CONTRACT_ROOT=/workspace/open-android-intelligence/gateway-contract \
  /workspace/gateway-sync/.venv-hermes311/bin/python -m pytest -q -ra tests \
  --ignore=tests/test_native_history_complete.py --ignore=tests/test_native_history_projection.py \
  --ignore=tests/test_native_streaming_delivery.py --ignore=tests/test_native_turn_lifecycle.py \
  --ignore=tests/test_native_turn_review_boundaries.py --ignore=tests/test_native_binding_http_failures.py \
  --ignore=tests/test_conversation_management.py --ignore=tests/test_host_command_catalog.py \
  --ignore=tests/test_android_echo_fixture.py

env HOME=/workspace/gateway-sync/home/hermes311-native \
  HERMES_HOME=/workspace/gateway-sync/home/hermes311-native/hermes \
  HERMES_TEST_ISOLATION=/workspace/gateway-sync/home/hermes311-native/hermes \
  TMPDIR=/workspace/gateway-sync/tmp/hermes \
  HERMES_SDK_ROOT=/workspace/open-android-intelligence/.toolchains/hermes-native-sdk \
  PYTHONPATH=/workspace/open-android-intelligence/.toolchains/hermes-native-sdk \
  OAI_REQUIRE_NATIVE_HERMES=1 \
  OPEN_ANDROID_GATEWAY_CONTRACT_ROOT=/workspace/open-android-intelligence/gateway-contract \
  /workspace/gateway-sync/.venv-hermes311/bin/python -m pytest -vv -ra -o faulthandler_timeout=30 \
  tests/test_native_history_complete.py tests/test_native_history_projection.py \
  tests/test_native_streaming_delivery.py tests/test_native_turn_lifecycle.py \
  tests/test_native_turn_review_boundaries.py tests/test_conversation_management.py \
  tests/test_host_command_catalog.py tests/test_native_binding_http_failures.py \
  tests/test_legacy_conversation_canonical.py tests/test_android_echo_fixture.py

OPEN_ANDROID_GATEWAY_CONTRACT_ROOT=/workspace/open-android-intelligence/gateway-contract \
  /workspace/gateway-sync/.venv-hermes311/bin/python scripts/check_plugin_manifest.py
TMPDIR=/workspace/gateway-sync/tmp/hermes \
  /workspace/gateway-sync/.venv-hermes311/bin/python -m build --no-isolation \
  --outdir /workspace/gateway-sync/tmp/hermes/dist
```

完整本地证据位于 `/workspace/gateway-sync/logs/hermes/`：`standalone-py311.log`、
`native-py311.log`、`standalone-py312.log`、`native-py312.log`、`contract-sync-py311.json`、
`official-manifest-py311.json`、`package-build-py311.log`、`package-check-py311.json`、
`wheel-smoke-py311.json`、`collection-coverage-py311.json`。没有执行 Gradle、远程推送、发布或标签操作；Android 真机验收未在本次插件同步中执行。
