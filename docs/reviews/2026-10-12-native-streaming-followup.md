# Hermes 原生流式回复补充排查与修复

本轮针对手机反馈“消息已有回复，但没有流式显示”补充排查。源码基线为插件 `746550a0138501e04de8730da8ea4c4c82d7293e`，已部署的 `d7ec5fbfae81cf9228fdd9d53c86e4c99d682b80` 与该基线的生产程序内容一致。两处宿主使用 Hermes `75b083e9399115b12513385252811f09c3503c90`。附件完整性失败由 Android 仓库的独立修复处理。

## 确认的接口错误

插件 `OpenAndroidPlatformAdapter.edit_message` 返回布尔值；当前 Hermes `BasePlatformAdapter.edit_message` 的原生契约返回 `SendResult`。`GatewayStreamConsumer` 和工具进度更新均读取 `result.success`，因此原生宿主将已发布的编辑误判为失败，产生 `'bool' object has no attribute 'success'`。

修复将成功与失败都改为 `SendResult`，成功回执包含同一个回复消息 ID。增量仍发布为 `conversation.message.delta`，最终编辑仍发布为 `conversation.message.completed`；没有新增协议版本、修改核心 Schema、复制宿主流式实现或改变账号、配对、会话绑定。

现有插件测试曾直接调用编辑入口并断言 `True`，未检查原生消费方的返回值契约。本次更新这些回归以检查结构化回执，并新增原生消费方的回归，避免用手工调用编辑入口代替宿主消费增量。

## 复现和验证边界

`tests/test_native_streaming_delivery.py` 使用实际 Hermes `GatewayStreamConsumer`、插件生产适配器、账号隔离的临时 Gateway、真实 Ed25519 签名及持续 SSE 订阅。在生成完成前依次提交三段测试文字，分别等待三个完整增量事件，最后等待同一消息 ID 的完成事件及宿主的投递确认。

- 初版只检查三个 SSE 增量时，原实现通过。此证据保存为 `evidence/2026-10-12-streaming/native-consumer-sse-baseline.txt`，它说明布尔返回值错误不会阻止编辑入口先发布字节。
- 增加原生消费方必须确认最终投递的断言后，原实现稳定失败：`final_response_sent=false`，四次出现布尔值没有 `success` 属性的错误。证据为 `native-consumer-red.txt`。
- 修正返回值后，该原生回归通过，`final_response_sent` 和 `delivered_final_matches` 均确认完成。证据为 `native-consumer-green.txt`。
- 原有 SSE、WebSocket 及回复身份回归合计 23 项通过，证据为 `transport-green.txt`。
- 新的逐段夹具在 SSE 与 WebSocket 两通道各通过一项回归，证据为 `stream-fixture-standalone.txt`。客户端确认收到当前片段之前，标准输入屏障阻止测试 Agent 生成后续片段及完成事件；没有用固定等待时间冒充生成期间的可观察性。
- 以当前真实 Hermes 源码及依赖重新执行原生 consumer、HTTP 批次与普通消息、完成 hook、原生历史及所有 echo/流式夹具，共 11 项通过，证据为 `native-integration-green.txt`。
- 最终独立 Python 全套为 338 项通过、2 项显式跳过，证据为 `plugin-full-final.txt`。这两项分别需要原生历史数据库及原生 consumer，已在上一条原生验证中实际执行通过。插件清单检查及差异空白检查通过。

原生测试允许普通独立测试环境显式跳过，但当调用方设置 `OAI_REQUIRE_NATIVE_HERMES=1` 时，缺少宿主必须失败，不能把跳过解释为原生验证通过。运行时需要以 `PYTHONPATH` 指向所验证的 Hermes 源码及其依赖，并将 `HERMES_HOME`、`HERMES_TEST_ISOLATION` 指向隔离目录。插件契约通过 `OPEN_ANDROID_GATEWAY_CONTRACT_ROOT` 显式指定本轮主仓契约。

## 回调接线与 Android 观察

检查实际宿主的 `gateway/run_turn_runner.py` 与 `gateway/run_turn.py`：正常用户回合按已加载的流式开关及平台显示覆盖创建 consumer，将 `stream_delta_callback` 绑定到 Agent，结束时把最终正文交给 `consumer.finish`。插件提供消息编辑方法，并声明 `REQUIRES_EDIT_FINALIZE=true`；因此即使最后一段内容没有变化，宿主仍须调用最终编辑并读取结构化回执。源码中没有为这个平台另外绕过增量回调的分支；这属于接线检查，不冒充调用真实模型的证据。

插件事件正文含 `conversationId`、`messageId`、`sender`、`parts`、`text`、正数时间及修订，Android 解码增量为 `TimelineUpsert(STREAMING)`，完成为 `TimelineUpsert(CONFIRMED)`。工作台按活动会话隔离事件，用消息身份更新显示；较旧修订及已确认回复的迟到增量有过滤规则。上述检查没有要求降低修订或会话隔离校验。

Android 仓库的 `HermesHttpInteropTest.kt` 增加两项互通回归，分别使用手机默认的 WebSocket 优先构造方式与明确 SSE 构造方式。测试通过生产网络客户端、签名、会话仓库和事件解码逐段观察 `STREAMING`，观察确认后才释放下一段生成，最终必须得到同一消息的 `CONFIRMED` 及用户消息的 `COMPLETED`。夹具按实际 HTTP 响应的 `101` 或 `200` 只记录通道类型，避免把默认 WebSocket 测试中的隐式 SSE 降级当作 WebSocket 通过。Android 的本机执行与 CI 结果由主任务另行归档，不包含在本报告的 Python 通过计数中。

## 两处部署的只读证据

仅解析流式开关并输出布尔值；未输出模型凭据或完整配置。两处配置都是 `streaming.enabled=true`，Android 的规范平台名及两个历史别名没有关闭流式的显示覆盖项。

服务器现存 Gateway 日志统计到 32 次相同的布尔回执错误，最近时间为 `2026-10-12 11:23:18,313`。这是实际运行中调用原生编辑消费路径的证据。

服务器账号数据库以只读方式按事件类型统计：自 `2026-10-11T00:00:00.000Z` 起有 20 条 `conversation.message.delta`、21 条 `conversation.message.completed`。未读取、解密或输出事件正文。这说明线上已有部分增量，不能把源码回执错误直接描述为所有增量均未发布，也不能据此声称手机逐步显示已经通过。

本轮未更改宿主配置，未重启服务，未操作手机。修复需要经过父任务的独立审查、CI 与部署后，再由用户对两处 Gateway 验证真实模型回复的逐步显示。确定性测试片段只证明原生消费方到事件传输的链路。
