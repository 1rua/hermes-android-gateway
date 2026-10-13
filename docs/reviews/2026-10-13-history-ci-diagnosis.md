# 历史修复 CI 停滞排查记录

## 已推送版本与 CI 状态

插件源码为 `6d14191296a27c8bc735bc80d88a6a147f92de65`，Android 源码为 `6594ccc316af826681b033e326fcb6311a3747fb`，原生 Hermes SDK 固定为 `75b083e9399115b12513385252811f09c3503c90`。本记录仅归档实际验证结果，不能代替最终 CI 通过或手机验收。

- [插件 CI 38369449540](https://github.com/1rua/hermes-gateway-plugin/actions/runs/38369449540) 的独立测试通过；原生历史步骤从 `2026-10-13T08:29:57Z` 开始，在第一个文件通过后没有继续输出，至 `08:49:37Z` 取消。其结论是 `cancelled`，不是通过。
- [Android CI 38370671396](https://github.com/1rua/open-android-intelligence/actions/runs/38370671396) 的 `Android Build & Check` 已通过；Node/契约任务的同一原生历史步骤在本记录写入时仍运行，整条 CI 尚未通过。
- 同一 Android 提交的 [APK 构建 38370671346](https://github.com/1rua/open-android-intelligence/actions/runs/38370671346) 和 [Nightly 38370671344](https://github.com/1rua/open-android-intelligence/actions/runs/38370671344) 通过。这两项不能覆盖停滞的原生测试。

本轮历史修复尚未部署到两处 Gateway。保留现有配置、账号、密钥、配对及宿主绑定；没有用生产数据重现此停滞。

## 隔离环境复核

从固定 SDK 的 Git 提交导出干净源码，排除本机虚拟环境、未跟踪文件与字节码。依赖目录和 Python 解释器也分别新建，没有覆盖用户已有解释器。三次均执行 `test_native_history_complete.py` 与 `test_native_history_projection.py` 的全部 23 项，未跳过原生测试。

| 环境 | 实际结果 | 原始日志 |
| --- | --- | --- |
| Python 3.11.15、pytest 8.4.2、干净 SDK | 23 passed，7.36 秒 | [日志](evidence/2026-10-13-history-refinement/clean-sdk-ci-repro.txt) |
| Python 3.11.15、与 CI 相同版本的独立依赖、pytest 9.1.1 | 23 passed，7.35 秒 | [日志](evidence/2026-10-13-history-refinement/clean-ci-deps-repro.txt) |
| Python 3.11.17、与 CI 相同版本的独立依赖、pytest 9.1.1 | 23 passed，7.41 秒 | [日志](evidence/2026-10-13-history-refinement/python31117-ci-repro.txt) |
| Python 3.11.17、独立 CI 依赖、默认输出捕获、完整四文件顺序 | 33 passed，21.51 秒 | [日志](evidence/2026-10-13-history-refinement/ci-capture-repro.txt) |

前三次使用 `-s` 输出诊断信息，不包含原生流式与 HTTP 夹具文件。第四次补齐原生流式与 HTTP 夹具，采用默认捕获与 CI 相同四文件顺序，33 项通过，未重现停滞。Python 与依赖版本相同不表示系统 SQLite 与执行环境也完全相同；因此不能据此认定 CI 为偶发问题，也不能把这些结果表述为 GitHub CI 通过。

两仓的原生步骤增加详细用例输出与 30 秒 faulthandler 调用栈，并限时 5 分钟；测试范围、断言与原生强制加载条件不变。本机第四次复核实际运行相同 pytest 参数。后续依据 CI 调用栈继续定位，只有找到实际原因、最小验证通过且最终 CI 通过后，才能更新部署记录。
