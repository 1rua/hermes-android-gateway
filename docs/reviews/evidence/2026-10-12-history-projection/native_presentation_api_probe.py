"""只使用原生宿主公共 API，验证展示侧记录不会改变模型重放。"""
from pathlib import Path
import json
import sys
import tempfile

from hermes_state import SessionDB
from gateway.run import _build_gateway_agent_history


def main(root):
    root.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="native-presentation-", dir=root)) / "native-presentation-probe.db"
    database = SessionDB(path)
    database.create_session("native_presentation", "open_android")
    database.append_message(
        "native_presentation", "user", "[合成模型上下文]\n原始图片提问",
        platform_message_id="cm_synthetic", timestamp=1,
    )
    database.append_message("native_presentation", "assistant", "合成回答", timestamp=2)
    before, _ = _build_gateway_agent_history(database.get_messages_as_conversation("native_presentation"))
    snapshot = {
        "version": 1, "accountId": "acct_synthetic", "conversationId": "conv_synthetic",
        "messageId": "msg_synthetic", "clientMessageId": "cm_synthetic", "sender": "user",
        "text": "原始图片提问", "parts": [{"type": "text", "text": "原始图片提问"}],
        "timestamp": 1000,
    }
    database.append_message(
        "native_presentation", "session_meta", None,
        platform_message_id="oai_display_synthetic", display_kind="oai_gateway_presentation",
        display_metadata={"oaiGatewayPresentation": snapshot}, timestamp=1,
    )
    after, _ = _build_gateway_agent_history(database.get_messages_as_conversation("native_presentation"))
    assert after == before
    assert len(after) == 2
    assert all(row["role"] in {"user", "assistant"} for row in after)
    database.close()
    database = SessionDB(path)
    try:
        rows = database.get_messages("native_presentation", include_inactive=True)
        display = next(row for row in rows if row.get("display_kind") == "oai_gateway_presentation")
        assert display["display_metadata"]["oaiGatewayPresentation"] == snapshot
        assert display["content"] is None
        restored, _ = _build_gateway_agent_history(database.get_messages_as_conversation("native_presentation"))
        assert restored == before
        print(json.dumps({
            "native_metadata_round_trip": True,
            "restart_retains_presentation": True,
            "model_replay_unchanged": True,
            "model_replay_rows": len(restored),
        }, ensure_ascii=False))
    finally:
        database.close()


if __name__ == "__main__":
    main(Path(sys.argv[1]))
