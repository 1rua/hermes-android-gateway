# 原生设备工具等待期限与请求终态修复

## 问题与证据

基线为 `f09951f1de80c294619d76001f9a7cf5eb8ebd36`。两个定向测试分别让设备不 Claim、Claim 后不回传；旧工具都忽略测试设定的短预算，停留在轮询等待中，超过 0.5 秒保护期限。实际源码将 read 队列最长 24 小时的期限直接当成 Agent 工具等待期限。

这是已复现的代码缺陷。截图那次故障缺少 `requestId` 与目标部署日志，用户明确表示亮屏且在前台，因此不能据此认定后台挂起。Android 配套修复及设备验收边界见关联 PR。

## 行为与兼容性

- 工具使用独立单调时钟，默认等待上限 60 秒；工具创建的请求期限最多 90 秒，为已领取请求留下有限的回传宽限，同时不超过风险原有更短上限。
- 未领取超时：取消请求，返回 `failed`、`DEVICE_OFFLINE` 或 `DEVICE_TIMEOUT`、`requestId`、`stage=claim_wait`。离线码依据 Gateway 当前在线登记，不能单凭此推断手机系统状态。
- 已领取超时：请求进入 `cancel_requested`，返回 `outcome_unknown`、`DEVICE_TIMEOUT`、`stage=result_wait`。宽限内允许真实结果回传，到期使用原状态机恢复；不把取消意图等同于未产生副作用。
- 90 秒是 Claim/回传的拒绝边界；数据库终态由请求访问或后台维护落盘。后台维护每 60 秒扫描一轮、每轮最多 10 个账号，不能声称所有账号都恰好在第 90 秒写入终态。
- 结果读取与超时取消共用事务；过期扫描在事务内重新读取状态，不覆盖扫描后完成的结果。
- 已取消或已过期请求释放请求事件正文；保留账号、配对、服务器收据和协议原有幂等语义。
- 授权变化与内部错误保留 `requestId` 和阶段，日志只输出状态/错误码/异常类型，不输出异常正文或通知内容。
- 没有修改公共 Schema、CORE 摘要、风险队列最大值或既有调用默认期限。

## 现场追踪

```bash
python scripts/trace_device_request.py --database <目标账号的gateway.sqlite> --request-id <devreq编号>
```

该工具使用 SQLite 只读连接及只读事务，不解密正文、不读取请求参数/凭据，不创建不存在的数据库。输出的 `awaiting_claim`、`awaiting_result`、`terminal` 用来定位缺失阶段，再与 Gateway `[open_android] device_request` 和 Android `OaiDeviceRequest` 的同一编号日志对齐。配对及授权修订一致性只代表数据库证据，不能代替手机通知监听器和系统权限现场状态。

## 验证与交付边界

短预算回归先红后绿。将基线 `recover_expired` 方法只恢复到测试进程后，竞争回归同样失败：候选扫描后已成功的请求被旧方法计为恢复 1 条，预期为 0；当前实现通过。只读诊断覆盖未领取、已领取、结果保存、撤权、脱敏、非法编号和不存在数据库。

测试使用 Python 3.11.15、独立虚拟环境，以及从 `contract-pin.json` 指定提交 `f0b3f1bd4f6f3301b820e38c9f3241bcac05cd10` 导出的干净契约。原生宿主使用 CI 固定提交 `75b083e9399115b12513385252811f09c3503c90` 的干净导出目录。运行前将 `OPEN_ANDROID_GATEWAY_CONTRACT_ROOT` 指向该导出的 `gateway-contract` 目录，原生矩阵同时设置 `HERMES_SDK_ROOT`、`PYTHONPATH` 和隔离的 `HERMES_HOME` / `HERMES_TEST_ISOLATION`。

独立插件完整矩阵：**447 passed，55.34 秒**。

```bash
python -m pytest -q -o faulthandler_timeout=30 tests \
  --ignore=tests/test_native_history_complete.py \
  --ignore=tests/test_native_history_projection.py \
  --ignore=tests/test_native_streaming_delivery.py \
  --ignore=tests/test_native_turn_lifecycle.py \
  --ignore=tests/test_native_turn_review_boundaries.py \
  --ignore=tests/test_native_binding_http_failures.py \
  --ignore=tests/test_conversation_management.py \
  --ignore=tests/test_host_command_catalog.py \
  --ignore=tests/test_android_echo_fixture.py
```

固定宿主原生矩阵：**92 passed，24.75 秒，无跳过**。

```bash
OAI_REQUIRE_NATIVE_HERMES=1 python -m pytest -q -o faulthandler_timeout=30 \
  tests/test_native_history_complete.py tests/test_native_history_projection.py \
  tests/test_native_streaming_delivery.py tests/test_native_turn_lifecycle.py \
  tests/test_native_turn_review_boundaries.py tests/test_conversation_management.py \
  tests/test_host_command_catalog.py tests/test_native_binding_http_failures.py \
  tests/test_legacy_conversation_canonical.py tests/test_android_echo_fixture.py
```

完整矩阵后补充“等待预算已耗尽时，已提交成功结果仍优先返回”的边界回归，并复核清理失败时保守保留 `outcome_unknown`。受影响矩阵最终 **113 passed，3.96 秒**，覆盖这次补充；未声称此后再次运行全部矩阵。

```bash
python -m pytest -q tests/test_device_request_timeouts.py \
  tests/test_trace_device_request.py tests/test_device_tools_complete.py \
  tests/test_review_regressions.py tests/test_capability_policy.py \
  tests/test_account_isolation.py tests/test_grant_revision_and_session_revoked_events.py \
  tests/test_pairing_revocation.py
git diff --check
```

独立代码审查与远程 CI 由主代理在提交推送后记录。

配套 `tests/device_capability_fixture.py` 也用上述虚拟环境启动独立子进程验证：随机本机端口启动成功，真实 HTTP 协商返回 200、密码登录返回 200，安全结束码为 0。未修改夹具的宿主版本校验；运行时须保留虚拟环境解释器路径，不能把符号链接解引用为缺少依赖的基础 Python。

未连接物理设备，未修改实际部署，不声称截图中那次请求已经完成现场闭环。需要目标部署的请求编号、加载提交和手机日志确认具体触发环节。
