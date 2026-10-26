"""按请求编号追踪状态，输出不得包含设备内容或身份凭据。"""

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest


spec = importlib.util.spec_from_file_location("trace_device_request", Path(__file__).parents[1] / "scripts" / "trace_device_request.py")
trace_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trace_module)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "gateway.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE device_requests(request_id, device_id, pairing_generation, grant_revision,
                risk, state, created_at, expires_at, result_json, parameters_json);
            CREATE TABLE device_keys(device_id, pairing_generation, grant_revision, public_key);
            CREATE TABLE claim_receipts(request_id, created_at, claim_id);
            CREATE TABLE audit_events(audit_id, event_type, occurred_at, subject_json);
            INSERT INTO device_requests VALUES('devreq_trace', 'private_device', 1, 2, 'read',
                'pending', '2026-10-24T00:00:00.000Z', '2026-10-24T00:01:30.000Z', '', 'private_parameters');
            INSERT INTO device_keys VALUES('private_device', 1, 2, 'private_key');
            INSERT INTO audit_events VALUES(1, 'device.request.enqueued', '2026-10-24T00:00:00.000Z',
                '{"requestId":"devreq_trace","extra":"private_audit"}');
        """)
    return path


def trace(database):
    return trace_module.trace_request(database, "devreq_trace", datetime(2026, 10, 24, tzinfo=timezone.utc))


def test_pending_trace_is_read_only_and_does_not_claim_delivery(database):
    before = database.read_bytes()
    result = trace(database)
    assert result["stage"] == "awaiting_claim"
    assert result["claimedAt"] is None
    assert result["pairingCurrent"] and result["grantCurrent"]
    assert result["timeline"][0]["stage"] == "device.request.enqueued"
    assert "private_" not in json.dumps(result)
    assert database.read_bytes() == before


def test_claim_result_and_revocation_are_distinguishable(database):
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO claim_receipts VALUES ('devreq_trace', '2026-10-24T00:00:01.000Z', 'private_receipt')")
        connection.execute("UPDATE device_requests SET state='claimed'")
    assert trace(database)["stage"] == "awaiting_result"
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE device_requests SET state='succeeded', result_json='private_notification'")
        connection.execute("UPDATE device_keys SET grant_revision=3")
    result = trace(database)
    assert result["stage"] == "terminal" and result["resultStored"]
    assert result["pairingCurrent"] and not result["grantCurrent"]
    assert "private_" not in json.dumps(result)


def test_missing_and_invalid_request_do_not_dump_other_records(database):
    assert trace_module.trace_request(database, "missing") == {"requestId": "missing", "requestFound": False}
    with pytest.raises(ValueError, match="REQUEST_ID_INVALID"):
        trace_module.trace_request(database, "' OR 1=1 --")


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        trace_module.trace_request(path, "devreq_trace")
    assert not path.exists()
