# Hermes 轻量入口独立审查

审查提交：`a4774a7e6a37841a34fbb2667bfd029fceafe0cc`；
基线：`b64dda00a0de9ff1a95a375dcf92f62b5a1027b3`。
结论：在本次轻量入口范围内未发现阻断问题。

23 个运行模块均为 Core/可选 Adapter 的转发或退出旧工具的说明，无自有设备状态机、
设备调度器或监听器。旧 `GatewayCore`、`DeviceRequestStore` 导入返回 Core 同一类。
根加载入口及包内注册入口均委托同一 `register`，manifest 不声明设备工具或钩子。

元数据只依赖 Core 和可选 Adapter 的 `>=0.2,<0.3` 版本；固定配套 OAI 提交
`1ecf7b8850c92f61a61662c49e9dbf25b24647de`（OAI `dev` 分支基线）中两包版本均为 `0.2.0`，
SDK pin 与 CI 的 `75b083e9399115b12513385252811f09c3503c90` 一致。
CI 从固定源码安装，不在运行时动态下载旧 Gateway。已更新 pin 锁定至基线提交并复核完成。

独立执行入口测试 `5 passed`、manifest 检查通过。核对本地 wheel 的 23 个模块均与
审查源码逐字节相同；wheel 和源码分发包均未包含 `legacy/`。历史脚本只供离线参考，
不进入新插件运行包。未发现新增真实凭据或含私密值的启动脚本。

本轮只读源码、检查分发包并使用合成环境测试，仅新增本报告，未提交或推送。
此结论不替代配套 Core 最终 SHA 的 CI、实际 Hermes 部署、真实模型或 Android 真机验收。
