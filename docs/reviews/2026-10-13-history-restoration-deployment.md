# 会话原稿、工具提示与图片恢复的最终交付记录

日期：2026-10-13。本轮承接用户手机截图：收到回复后切换会话再返回，用户原稿被模型图片说明替换、工具进度出现空文本占位、图片不能再次预览。用户已确认侧边栏退出、普通回复和本机附件上传及真实 Agent 接收恢复；服务器附件、手机可见流式和本轮历史恢复仍待两处手动确认。

## 已提交并推送的固定版本

| 对象 | 提交 |
| --- | --- |
| Android 最初历史程序修复 | `6594ccc316af826681b033e326fcb6311a3747fb` |
| Android 最终 CI 输入、互通记录、APK 与 Nightly | `d0fbfaeb9c0acda04dbe72fa9306d9109c54bdf1` |
| 插件程序、Android CI 固定插件、两处实际安装 | `9aa6a0661e76c31c2b1078890a4b48ef002fee10` |
| 两处现有 Hermes SDK | `75b083e9399115b12513385252811f09c3503c90` |

本交付记录后续的归档提交只改变文档与真实证据。两处安装继续运行已验证的 `9aa6a06`，Android 的固定实现引用保持该不可变提交，不因文档归档前移。主仓说明见[Android 交付报告](https://github.com/1rua/open-android-intelligence/blob/d0fbfaeb9c0acda04dbe72fa9306d9109c54bdf1/docs/reviews/2026-10-13-history-restoration-delivery.md)。

## 修复与已执行验证

宿主标准 KV 持有原始发送正文、附件身份、已发布工具/命令提示及可靠的单条正文修订；展示数据不进入模型消息物理尾部。投递前保留上传原件，历史媒体接口继续遵守账号、会话、设备、授权与撤回限制。Android 按稳定身份恢复发送记录，合并明确的新正文修订，并在有限、隔离的缩略图缓存中支持 A → B → A。明确删除不能由本地发送记录或迟到回执恢复。

独立审查在先前版本发现一项 P1、四项 P2，均已补修。用户随后明确要求不使用子 Agent，后续由主 Agent 独立复核，未宣称新增独立零问题复审。CI 诊断又捕获真实宿主写锁重入：清理回调已经持锁，却调用独立读取 API；读取回退到写锁时停滞。原实现新增回归确实超时，补修复用当前事务连接，保持模型行及其他账号、其他存储归属。[锁补修与原始失败证据](2026-10-13-history-native-cleanup-lock.md)保留，失败、取消及失效临时契约路径的中间结果没有改写成通过。

| 层次 | 实际通过结果 |
| --- | --- |
| Android 发送、删除、镜像、预览与解码最小回归 | 112 项；程序自 `6594ccc` 后未再变化 |
| Android 时间线及原身份查询字段透传 | 14 项 |
| 最终确切插件上的生产 Android HTTP 客户端 | 5 项，关闭构建缓存、相关 35 个任务全部实际执行 |
| 独立插件完整测试 | 341 项，0 失败/跳过 |
| 强制实际 SDK、原生历史、迁移、读取回退、流式及 HTTP | 34 项，0 失败/跳过 |
| 旧 SQLite 3.45.0 测试进程对照 | 24 项，0 失败/跳过；不是生产依赖修改 |
| 主仓共享协议向量及安装声明 | 66 / 66、manifest/入口/协议通过 |

存在重叠用例，不将这些数量相加为独立测试总数。模型边界采用确定性隔离处理器，自动化验证不能替代两处用户账号的真实模型与手机界面。源码与原始日志分别在所属仓库报告中归档，最终跨仓输入见[5 项 HTTP 的源码摘要](https://github.com/1rua/open-android-intelligence/blob/d0fbfaeb9c0acda04dbe72fa9306d9109c54bdf1/docs/reviews/evidence/2026-10-13-history-http/android-native-http-final-9aa6a06-source.json)。

## 完整 CI

| 流水线 | 结果 |
| --- | --- |
| 插件 `9aa6a06` | [38394963723 通过](https://github.com/1rua/hermes-gateway-plugin/actions/runs/38394963723) |
| Android `d0fbfae`：Node/跨宿主门禁与完整 Gradle check | [38395462724 通过](https://github.com/1rua/open-android-intelligence/actions/runs/38395462724) |
| 同提交 FullDebug APK | [38395462723 通过](https://github.com/1rua/open-android-intelligence/actions/runs/38395462723) |
| 同提交 Nightly | [38395462666 通过](https://github.com/1rua/open-android-intelligence/actions/runs/38395462666) |

四份成功元数据保存在[最终交付证据目录](evidence/2026-10-13-history-release/)。本机只执行本轮必要定向 Gradle 测试，没有执行全量 Gradle 或 APK 构建。

## 两处实际更新

两仓完整 CI 通过后，将官方源码仓与两处既有插件安装快进到 `9aa6a06`，重启各自现有的用户 `hermes-gateway.service`，没有建立新服务、重建账号、解除配对或清理数据。服务器的系统同名单元处于 inactive；监听进程所属 cgroup 确认实际服务是 root 的用户单元，已按该归属重启。首次服务器临时部署脚本因系统 Python 3.6 不支持 `text` 参数在 Git 更新前停止，改为兼容参数后成功；没有升级 Hermes SDK 或更改程序依赖。

| 部署 | 程序版本 | 原服务与健康检查 |
| --- | --- | --- |
| 本机 | `8a883b9` → `9aa6a06` | PID 920346，2026-10-13 20:22:11 北京时间；active/running，退出状态 0；原端口 11451，HTTP 200 |
| `47.251.8.196` | `8a883b9` → `9aa6a06` | PID 2027451，2026-10-13 20:26:14 北京时间；active/running，退出状态 0；原端口 8045，HTTP 200，公开健康地址亦 HTTP 200 |

两处安装工作区干净；各自三份配置、环境和服务文件以及一份既有主密钥在更新与重启前后摘要一致。只发布文件数量及一致性布尔值，不公布敏感内容或文件摘要；[实际部署记录](evidence/2026-10-13-history-release/deployments.json)保留。健康与摘要验证证明程序更新和配置保留，不证明所有账号历史或手机场景验收通过。

核心 Schema 仍为 `sha256:99309b87dec79f9c737b7d875a6e949ceffb6bb18b468d7ce98ca37d77dd3d50`，Protocol 2.1、附件加密格式、配对与宿主绑定保持。旧版本已释放的原图片字节无法凭描述重建；普通模型改写不会作为用户编辑，聚合输入任意编辑没有逐成员拆分契约。原件上传及交付可以超过 25 MiB，既有历史 content 接口的 25 MiB 上限继续如实呈现。

## 匹配 APK 与手动验收

[重新下载 d0fbfae APK](https://github.com/1rua/open-android-intelligence/releases/download/nightly/app-full-debug.apk)。Nightly 目标提交、官方资产摘要与本机从 APK 工作流取得的完整文件均已核对：大小 61,781,254 字节；SHA-256 `853f80d8adcd435c1cafd47ae5a85780527f7bfe75bea0d1dafcec1bb57b0236`。固定调试证书 SHA-256 `37d9445fab11d827b032b84b0739acb2c33aa8ace1f319ed20a4cc612624ce33`，与原调试包一致，可覆盖安装保留本地数据。[APK 坐标](evidence/2026-10-13-history-release/source-apk-coordinates.json)及[官方资产元数据](evidence/2026-10-13-history-release/nightly-asset.json)保留；滚动 Nightly 以后可能前移，确切来源以上述工作流为准。

手机验收仍待用户反馈，Agent 没有执行 ADB、操作手机或发送生产账号消息。两处分别覆盖安装本 APK、重新上传并发送一张图片/文件，收到含工具进度与最终正文的真实回复后切换到其他会话再返回，再清理后台重新进入，检查原文、工具提示、图片预览和消息保留；另检查较长回复完成前逐段显示及服务器 Agent 实际收到附件。最后从侧边栏退出并重启，确认登录页与不自动恢复已退出账号。

只在两处上述手机结果反馈通过后，才将本轮与原三项问题标记为完整修复闭环。
