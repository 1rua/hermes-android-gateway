# 附件与流式补修的跨仓交付记录

日期：2026-10-12。本轮承接 `f82fc33` 手机反馈：用户已确认侧边栏退出和普通消息回复恢复；两处的所有附件仍被“附件完整性校验失败”拒绝，回复未逐步显示。此记录区分源码、真实 HTTP、CI、部署与手机验收，未提前报告整体闭环。

## 固定版本与兼容

| 对象 | 修订 |
|---|---|
| Android 程序补修及完整 CI | `9dc4cc9446ab27b531ac4f2f09f9e3adb2a82139` |
| Android 最终归档、APK 与 Nightly | `d5e1838a04e8508845bb4fc6529e8edfcb776330` |
| Android CI 固定插件与两处实际安装 | `8a883b907dd46704de2946ff0e4edec26ed92d1f` |
| 两处实际 Hermes 宿主 | `75b083e9399115b12513385252811f09c3503c90` |

Android 最后归档只增加文档与证据；程序、测试、CI 固定插件及核心 Schema 不变。此报告与两份插件审查报告的归档提交也只改变文档，两处安装继续运行已验证的 `8a883b9`。Gateway Protocol 仍为 2.1，核心摘要 `sha256:99309b87dec79f9c737b7d875a6e949ceffb6bb18b468d7ce98ca37d77dd3d50`。

本轮没有附件格式、账号、密钥、配对、凭据或历史迁移。没有改变流式开关、模型配置、宿主版本或现有服务名称；插件程序依赖未变化。

## 修复与真实证据

Android 内容 PUT 原先遗漏创建附件时声明的媒体类型。实际 Android 网络类自动补入的表单类型导致 Hermes 在正确正文、长度和 SHA-256 下仍返回 `ATTACHMENT_DIGEST_MISMATCH`。生产上传器显式传入附件的 `Content-Type`；没有放宽摘要、长度或媒体类型校验，也没有修改前序 Keystore 加密修复。Android 的平台差分先复现 4 项中的 3 项失败，补头后相关 29 项通过；另外 15 项连接安全和 4 项跨端互通通过，共 48 项不同定向回归。

插件原生编辑返回布尔值，而 Hermes 消费方读取 `SendResult.success`。补修返回结构化回执及同一回复 ID，完成投递得到原生确认。独立 Python 全套 338 通过、2 个原生依赖跳过；真实宿主专项 11 项通过，其中实际执行两项原生依赖。旧实现已经能先发部分增量，故这一错误不能被认定为所有手机无流式的唯一原因。详见 [流式排查与修复](2026-10-12-native-streaming-followup.md)。

Android 默认 WebSocket 和明确 SSE 的互通回归，均在每段非空增量实际被客户端观察后才释放下一段生成，最终核对同一消息完成及用户消息完成。实际宿主和独立 CI 依赖环境分别追加执行，包含图片、PDF、空文件上传与两通道真实 HTTP；重复项目不累加进 48 项。确定性夹具不替代用户账号的模型回复及手机显示。

Android 全部说明及日志已提交至 [附件与流式补修记录](https://github.com/1rua/open-android-intelligence/blob/d5e1838a04e8508845bb4fc6529e8edfcb776330/docs/reviews/2026-10-12-attachment-streaming-followup.md)。其中如实保存首次 App 任务 `UP-TO-DATE`，随后使用每任务 `--rerun` 实际重跑两任务 8 项通过，不把缓存结果当新证据。

## 独立审查与 CI

插件程序推送后两名独立审查者分别完成 [Standards](2026-10-12-streaming-post-push-standards.md) 和 [Spec](2026-10-12-streaming-post-push-spec.md)，均无确证发现，各自实际运行原生 consumer 与两通道夹具 3 项通过。

Android 首次增量审查及 CI 发现真实平台类和通道名触发源码门禁。补修仅允许两个完整 JVM 测试路径的出站断言，仍检查所有通用禁令；三处负对照分别证明指定测试中的 VPN、其他测试及生产文件的网络关键字仍被拒绝。TLS 职责文案也已纠正。后续推送后的 [独立双轴复审](https://github.com/1rua/open-android-intelligence/blob/d5e1838a04e8508845bb4fc6529e8edfcb776330/docs/reviews/2026-10-12-app-source-guard-post-push-review.md) 无新增确证问题。最后 `9dc4cc9...d5e1838` 的文档增量另经独立只读复核，确认仅 6 份文档、真实执行与缓存边界、8 项不重复计数及所有运行版本一致，无确证问题。

| CI | 结果 |
|---|---|
| 插件程序 `8a883b9` | [38201747552 通过](https://github.com/1rua/hermes-gateway-plugin/actions/runs/38201747552) |
| Android 程序 `9dc4cc9` 完整检查 | [38227153418 通过](https://github.com/1rua/open-android-intelligence/actions/runs/38227153418) |
| Android 最终归档 `d5e1838` 完整检查 | [38228743834 通过](https://github.com/1rua/open-android-intelligence/actions/runs/38228743834) |
| `d5e1838` FullDebug APK | [38228743823 通过](https://github.com/1rua/open-android-intelligence/actions/runs/38228743823) |
| `d5e1838` Nightly | [38228743803 通过](https://github.com/1rua/open-android-intelligence/actions/runs/38228743803) |

两次 Android 完整检查均包含 Node/跨宿主契约和 Android Build & Check；最后成功元数据见 [CI 原始记录](evidence/2026-10-12-streaming/app-final-ci.json)。本机未执行全量 Gradle `check` 或 APK 构建。

## 两处实际部署

经过程序完整 CI 与独立审查后，以完整 Git bundle 将既有插件安装从 `d7ec5fb` 快进至 `8a883b9`，再重启原 `hermes-gateway.service`。没有覆盖配置，没有新建服务，没有执行账号重建、数据库删除、配对解除或密钥清理。

| 部署 | 新服务状态 | 健康检查 |
|---|---|---|
| 本机 | PID 509096，2026-10-12 17:18:34 北京时间启动；active/running，退出状态 0 | 原端口 11451，HTTP 200 / ok |
| `47.251.8.196` | PID 1374718，2026-10-12 17:20:28 北京时间启动；active/running，退出状态 0 | 原端口 8045，HTTP 200 / ok；公开地址亦通过 |

两处插件工作区干净。各自三份配置、环境和服务文件在更新、重启前后摘要一致；只归档文件数量和一致性布尔值，不发布内容或摘要。证据来自实际更新及重启脚本成功返回的 JSON，见 [部署原始记录](evidence/2026-10-12-streaming/deployment.json)。这些记录不等同于账号历史、真实模型、附件接收和手机验收通过。

## APK 与待手机验收

[重新下载本轮 APK](https://github.com/1rua/open-android-intelligence/releases/download/nightly/app-full-debug.apk)。当前 Nightly 目标为 `d5e1838`；制品大小 61,715,717 字节，SHA-256 为 `6089e4a25674f5745a08da187a268c3aa129853dc5e5a8e904cdf45ab24e25d7`。固定调试证书 SHA-256 为 `37d9445fab11d827b032b84b0739acb2c33aa8ace1f319ed20a4cc612624ce33`，本机签名再校验通过，可覆盖安装同签名旧版。

`9dc4cc9` 下载的完整制品与 `d5e1838` Nightly 的官方资产摘要完全相同。重复下载受网络中断影响后，保留部分文件在回收暂存目录，以完整同摘要制品恢复本地副本，再检查完整摘要和签名；没有使用受损下载。滚动 Nightly 以后可能前移，确切版本以以上修订和流水线为准。

已通过异步反馈入口提供新 APK 和以下两处手动验证步骤，Agent 未执行 ADB 或手机界面操作：

1. 重新下载并覆盖安装后，移除旧的失败附件卡片，再重新选择图片或文件上传并发送，确认真实 Agent 收到。
2. 请求较长回复，确认最终完成前能逐段看到正文。
3. 回复前切换会话并返回，以及清理后台再进入，确认用户消息保留且不串会话。

用户已确认退出及普通回复恢复。附件、可见流式和消息保留仍等待本轮两处手机反馈；未收到这些结果之前，整个任务保持待验收状态。
