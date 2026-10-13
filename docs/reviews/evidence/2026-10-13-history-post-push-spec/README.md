# 独立规格审查证据

所有生产源码固定为插件 `a4a6acc855e2499ef11857a0f9ca438e7f424f63`，宿主为 `75b083e9399115b12513385252811f09c3503c90`。脚本只创建合成账号、临时 SessionDB 与合成文件，不访问实际账号或服务，也不调用永久删除命令。

| 文件 | 实際内容 |
| --- | --- |
| `repro_native_deletion_before_read.py` / `native-deletion-before-read.json` | 原生明确撤回之后，时间线读取前后的 metadata、新旧 grant 与原件下载结果 |
| `repro_equal_reply_identity.py` / `equal-reply-identity.json` | 旧未映射原生回答与新 STREAMING 通知相同，旧原生行被赋予新 messageId 及新时间 |
| `repro_native_edit.py` / `native-explicit-edit.json` | 明确软归档替换旧 user、保持平台身份后的新 active 内容及返回的旧展示内容 |
| `native-selected-independent.txt` | 独立选定原生集 14 passed、1 deselected |
| `native-focused-independent.txt` | 包含大附件专项的首次独立运行被审查者中断，只有进度，无完整结论，不计通过 |

按当前环境安装最小依赖并提供 SDK 后可分别执行探针：

```bash
PYTHONPATH="$TEST_DEPENDENCIES:$HERMES_SOURCE" PYTHONDONTWRITEBYTECODE=1 \
  "$PYTHON_311" -B -s docs/reviews/evidence/2026-10-13-history-post-push-spec/repro_native_deletion_before_read.py
PYTHONPATH="$TEST_DEPENDENCIES:$HERMES_SOURCE" PYTHONDONTWRITEBYTECODE=1 \
  "$PYTHON_311" -B -s docs/reviews/evidence/2026-10-13-history-post-push-spec/repro_equal_reply_identity.py
PYTHONPATH="$TEST_DEPENDENCIES:$HERMES_SOURCE" PYTHONDONTWRITEBYTECODE=1 \
  "$PYTHON_311" -B -s docs/reviews/evidence/2026-10-13-history-post-push-spec/repro_native_edit.py
```

独立选定集使用包含 SDK 的 `PYTHONPATH`、显式 Gateway 契约目录、独立 `HERMES_HOME/HERMES_TEST_ISOLATION`、`OAI_REQUIRE_NATIVE_HERMES=1` 与新建 `--basetemp`，运行：

```bash
"$PYTHON_311" -B -s -m pytest -q \
  tests/test_native_history_projection.py tests/test_native_history_complete.py \
  -k 'not large_uploaded' --basetemp="$TEST_TEMP_ROOT/pytest" --tb=short
```

SDK 源码语义核对：`hermes_state_messages.py:549–567` 将 `archive_dropped=True` 定义为用户 rewind/edit/regenerate 的软归档模式；`tui_gateway/methods_prompt.py:356–437` 经过 `confirm_truncate`、目标行核验和正在运行回合的锁保护后调用该模式。普通 Gateway `rewrite_transcript` 在 `gateway/session_transcript.py:482–509` 调用时不传 `archive_dropped`；`gateway/run_turn.py:1070–1110` 原位压缩通过 `archive_and_compact` 保留 `compacted=1`，轮换压缩只重写新会话。编辑探针保留相同 `platform_message_id`；实际 TUI 先切断旧回合再新提交，未必维持该身份。不能把任意 `content` 差异、普通模型整形、全页 `snapshotRevision` 当作明确编辑事件。
