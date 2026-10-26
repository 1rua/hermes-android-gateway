#!/usr/bin/env python3
"""只读追踪单次设备请求；不读取通知正文、请求参数或认证凭据。"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3


def trace_request(database: Path, request_id: str, now: datetime | None = None) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9._~-]{1,128}", request_id):
        raise ValueError("REQUEST_ID_INVALID")
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        row = connection.execute(
            "SELECT request_id, device_id, pairing_generation, grant_revision, risk, state, "
            "created_at, expires_at, result_json != '' AS result_stored "
            "FROM device_requests WHERE request_id=?", (request_id,),
        ).fetchone()
        if row is None:
            return {"requestId": request_id, "requestFound": False}
        receipt = connection.execute(
            "SELECT created_at FROM claim_receipts WHERE request_id=?", (request_id,),
        ).fetchone()
        binding = connection.execute(
            "SELECT pairing_generation, grant_revision FROM device_keys WHERE device_id=?",
            (row["device_id"],),
        ).fetchone()
        terminal = row["state"] in {"succeeded", "failed", "denied", "cancelled", "expired", "outcome_unknown"}
        current = now or datetime.now(timezone.utc)
        expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
        timeline = connection.execute(
            "SELECT event_type, occurred_at FROM audit_events "
            "WHERE event_type IN ('device.request.enqueued', 'device.request.claimed', "
            "'device.request.result', 'device.request.expired') "
            "AND json_extract(subject_json, '$.requestId')=? ORDER BY audit_id", (request_id,),
        ).fetchall()
        return {
            "requestId": request_id,
            "requestFound": True,
            "state": row["state"],
            "risk": row["risk"],
            "stage": "terminal" if terminal else "awaiting_result" if receipt else "awaiting_claim",
            "createdAt": row["created_at"],
            "expiresAt": row["expires_at"],
            "deadlinePassed": current >= expires,
            "claimedAt": receipt["created_at"] if receipt else None,
            "resultStored": bool(row["result_stored"]),
            "pairingCurrent": binding is not None and binding["pairing_generation"] == row["pairing_generation"],
            "grantCurrent": binding is not None and binding["grant_revision"] == row["grant_revision"],
            "timeline": [{"stage": event["event_type"], "at": event["occurred_at"]} for event in timeline],
        }
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path, help="当前部署账号的 gateway.sqlite 路径")
    parser.add_argument("--request-id", required=True)
    args = parser.parse_args()
    try:
        result = trace_request(args.database, args.request_id)
    except (OSError, sqlite3.Error, ValueError):
        # 不输出底层异常，防止路径、SQL 或其他本机资料进入诊断附件。
        print(json.dumps({"error": "TRACE_UNAVAILABLE", "hint": "请检查数据库路径、只读权限、版本和 requestId 格式"}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["requestFound"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
