# 三项缺陷修复的 CI、部署与手机验收记录

日期：2026-10-12。此记录覆盖独立插件程序更新与跨仓交付，不把服务健康检查或隔离回答当作手机真实模型验收。

## 确切版本

| 对象 | 提交 |
| --- | --- |
| Android 交付与 APK | `f82fc3390911b6aec4c1ed8b967225b1d755916b` |
| Android 产品逻辑最终修复 | `0595294ac2f9cbfb9464b5debc40c253a2aa70d9` |
| 插件程序修复 | `9f6878b42e8ef73a6a6d8f0e9a41d48a8230c514` |
| Android CI 固定插件及两处实际安装 | `d7ec5fbfae81cf9228fdd9d53c86e4c99d682b80` |
| 两处 Hermes 宿主源码 | `75b083e9399115b12513385252811f09c3503c90` |

本报告后续提交只增加文档与汇总证据；两处服务继续运行以上已验证的 `d7ec5fb` 程序。核心 Schema 没有变化，仍为 Gateway Protocol 2.1，摘要 `sha256:99309b87dec79f9c737b7d875a6e949ceffb6bb18b468d7ce98ca37d77dd3d50`。

## 源码、互通与审查

插件完整单元与边界回归为 336 通过、1 个原生环境专用跳过；真实 Hermes 另有 8 项通过。插件两个请求断点和异步宿主 hook 的独立审查均无剩余问题，详见本仓 [修复记录](2026-10-10-message-batch-dispatch-bugfix.md)、[规范轴](2026-10-11-batch-post-push-standards.md)、[规格轴](2026-10-11-batch-post-push-spec.md)。

Android 前序定向回归共 215 项通过；查询结果边界追加验证分阶段为客户端 16、工作台 52、数据 12 项通过，分阶段执行不重复累计为新的总数。生产 Android HTTP 客户端与真实原生 Hermes 的两个互通用例通过，确定性 handler 只替换模型边界。旧检查点迁移、会话隔离、迟到回执、取消终态、空快照、编辑及删除重放均有回归。应用报告与证据已提交在 [Android 修复报告](https://github.com/1rua/open-android-intelligence/blob/f82fc3390911b6aec4c1ed8b967225b1d755916b/docs/reviews/2026-10-11-conversation-bugfix.md)，含两路正式独立审查及迁移说明。

最后 CI 曾因测试任务未安装 Hermes 宿主提供的 `aiohttp`，无法启动 echo 夹具而失败。干净 Python 3.11 环境复现实际启动错误，补齐依赖后两个完整 HTTP/SSE 用例通过，未跳过或放宽断言。

推送后的 `0595294...f82fc33` 最终增量另由两名独立 Agent 审查：规范轴 0 发现、规格轴 0 发现。规范轴只读核对代码、隔离边界与实际测试 XML；规格轴独立以 Python 3.11.15、aiohttp 3.14.5 启动固定插件的隔离生产适配器，健康检查通过。该独立 CLI 不冒称完整 HTTP/SSE 重跑，两轴均未将生产模型及手机验收标记为通过。

## CI 与 APK

| 流水线 | 结果与证据 |
| --- | --- |
| 独立插件修复 CI | [通过](https://github.com/1rua/hermes-gateway-plugin/actions/runs/38070753720) |
| 插件含独立审查提交 CI | [通过](https://github.com/1rua/hermes-gateway-plugin/actions/runs/38084443175) |
| Android 完整 CI：Gradle check 与 Node/跨宿主契约门禁 | [通过](https://github.com/1rua/open-android-intelligence/actions/runs/38197739322) |
| Android FullDebug APK 与固定签名校验 | [通过](https://github.com/1rua/open-android-intelligence/actions/runs/38197739261) |
| 对应 Nightly 发布 | [通过](https://github.com/1rua/open-android-intelligence/actions/runs/38197739263) |

本机已下载并再校验 APK，与固定 Debug 证书一致，供同签名安装覆盖使用。产物大小为 61,715,709 字节；APK SHA-256 为 `9acae9f8df8c88ed8a2e590a55eba8f603aa3544c9e9f854ba9e913c86e4042b`，与 Nightly 资产摘要一致。证书 SHA-256 为 `37d9445fab11d827b032b84b0739acb2c33aa8ace1f319ed20a4cc612624ce33`。

[直接下载 APK](https://github.com/1rua/open-android-intelligence/releases/download/nightly/app-full-debug.apk)，Nightly 当前目标提交为 `f82fc33`。滚动标签以后可能前移；确切构建记录以上表的 APK 流水线为准。

## 两处部署结果

CI 与审查通过后，通过完整 Git bundle 更新既有插件安装，仅快进程序相关文件；没有建立新服务，没有执行账号重建、数据库删除、配对解除或主密钥清理。更新没有新增程序依赖，现有 Hermes 宿主已具备 aiohttp；两处宿主程序没有升级。

| 部署 | 更新前 | 更新后 | 原服务与健康检查 |
| --- | --- | --- | --- |
| 本机 | `7fc91869e0494316b78ef254e2560ff4556fb658` | `d7ec5fb` | 原 `hermes-gateway.service` 重启，PID 63304，2026-10-12 11:04:02 CST 启动；端口 11451，`/health` 为 HTTP 200 / ok |
| `47.251.8.196` | `a41e692dc640d6ef94b8083bf220f260936234dd` | `d7ec5fb` | 原 `hermes-gateway.service` 重启，PID 1253178，2026-10-12 11:05:06 CST 启动；端口 8045，`/health` 为 HTTP 200 / ok |

两处服务均 `active/running`、`ExecMainStatus=0`，插件工作区干净。分别对实际存在的 3 份配置、环境及服务文件进行更新前后摘要比对，均未变化；摘要与敏感文件内容没有公布。服务器原有公开地址 `http://47.251.8.196:8045/health` 亦返回 HTTP 200 / ok。汇总见 [部署证据](evidence/2026-10-12-deployment.json)。

这些操作验证程序版本、既有服务启动和配置保留；现有账号的真实消息、宿主历史和附件接收继续由以下手机验收确认。

## 手机验收状态

状态：**等待用户手动反馈，未报告整体修复闭环完成**。用户已得到 APK 及验收入口，Agent 未操作手机界面或执行 ADB 安装、账号数据清理。

两处 Gateway 分别完成：

1. 覆盖安装对应 APK，用原账号发送普通文本并收到真实 Agent 回复。
2. 回复前切换会话再返回；清理后台再进入，用户消息仍保留且不串会话。
3. 上传并发送图片或文件，确认真实 Agent 收到附件。
4. 从侧边栏退出，回到登录页；重启不自动恢复已退出账号。

只有两处的上述手机验收全部反馈通过后，才能将三项缺陷标记为完整闭环。
