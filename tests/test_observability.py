"""Request IDs and structured logs (B30), and the client error endpoint (B32)."""
from __future__ import annotations

import io
import json
import logging
import uuid

import pytest

from app.observability import JsonFormatter, TextFormatter, configure_logging, new_request_id
from harness.client import FakeAgent

FIELDS = {"ts", "level", "logger", "request_id", "method", "path", "status", "duration_ms", "user", "node"}


@pytest.fixture
def request_lines(caplog):
    caplog.set_level(logging.INFO, logger="lab.request")

    def lines() -> list[logging.LogRecord]:
        return [r for r in caplog.records if r.name == "lab.request"]

    return lines


def test_a_request_id_is_made_when_absent_and_echoed_when_given(client):
    first = client.get("/auth/config")
    assert len(first.headers["X-Request-ID"]) == 32
    assert client.get("/auth/config").headers["X-Request-ID"] != first.headers["X-Request-ID"]
    given = client.get("/auth/config", headers={"X-Request-ID": "portal-7f3a.9"})
    assert given.headers["X-Request-ID"] == "portal-7f3a.9"


@pytest.mark.parametrize("bad", ["has space", "x" * 200, "semi;colon", "new\tline"])
def test_an_unusable_request_id_is_replaced(client, bad):
    got = client.get("/auth/config", headers={"X-Request-ID": bad}).headers["X-Request-ID"]
    assert got != bad and len(got) == 32
    assert new_request_id(None) != new_request_id(None)


def test_errors_carry_the_request_id_too(client):
    resp = client.get("/projects", headers={"X-Request-ID": "err-1"})
    assert resp.status_code == 401 and resp.headers["X-Request-ID"] == "err-1"


def test_one_log_line_per_request_with_the_fields(client, request_lines):
    email = f"obs-{uuid.uuid4().hex[:6]}@uni.edu"
    resp = client.get("/projects", headers={"X-Dev-User": email, "X-Request-ID": "obs-1"})
    [record] = [r for r in request_lines() if r.fields["request_id"] == "obs-1"]
    entry = json.loads(JsonFormatter().format(record))
    assert set(entry) == FIELDS
    assert entry["request_id"] == "obs-1" and entry["method"] == "GET" and entry["path"] == "/projects"
    assert entry["status"] == resp.status_code == 200
    assert entry["user"] == email and entry["node"] is None
    assert entry["duration_ms"] >= 0 and entry["level"] == "info" and entry["logger"] == "lab.request"
    assert entry["ts"].endswith("+00:00")


def test_the_log_line_names_the_node_and_the_status_of_a_refusal(client, request_lines):
    tag = f"obs-{uuid.uuid4().hex[:8]}"
    agent = FakeAgent(
        client, hostname=f"host-{tag}", caps={"compatibility_class": tag, "gpu_model": tag, "vram_total_mb": 0, "vram_allocatable_mb": 0}
    )
    agent.register()
    client.post("/agent/heartbeat", json={}, headers={**agent._headers(), "X-Request-ID": "obs-node"})
    [record] = [r for r in request_lines() if r.fields["request_id"] == "obs-node"]
    assert record.fields["node"] == f"host-{tag}" and record.fields["user"] is None
    client.get("/projects", headers={"X-Request-ID": "obs-401"})
    [refused] = [r for r in request_lines() if r.fields["request_id"] == "obs-401"]
    assert refused.fields["status"] == 401


def test_the_query_string_stays_out_of_the_log(client, request_lines):
    client.get("/auth/config?token=secret", headers={"X-Request-ID": "obs-q"})
    [record] = [r for r in request_lines() if r.fields["request_id"] == "obs-q"]
    assert record.fields["path"] == "/auth/config" and "secret" not in JsonFormatter().format(record)


def test_log_lines_made_during_a_request_carry_its_id(client):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("lab.client")
    logger.addHandler(handler)
    try:
        client.post("/client-errors", json={"message": "x"}, headers={"X-Request-ID": "obs-c"})
    finally:
        logger.removeHandler(handler)
    [line] = stream.getvalue().splitlines()
    assert json.loads(line)["request_id"] == "obs-c"


def test_the_formatters_follow_log_format():
    record = logging.LogRecord("lab", logging.WARNING, "f.py", 1, "disk %s", ("low",), None)
    entry = json.loads(JsonFormatter().format(record))
    assert entry["message"] == "disk low" and entry["level"] == "warning"
    text = TextFormatter("%(levelname)s %(message)s").format(record)
    assert text == "WARNING disk low"

    lab = logging.getLogger("lab")
    before = list(lab.handlers)
    try:
        configure_logging("json")
        assert isinstance(lab.handlers[-1].formatter, JsonFormatter)
        configure_logging("text")
        ours = [h for h in lab.handlers if h.get_name() == "lab-structured"]
        assert len(ours) == 1 and isinstance(ours[0].formatter, TextFormatter)
    finally:
        lab.handlers = before


def test_the_production_compose_logs_json():
    from pathlib import Path

    compose = (Path(__file__).resolve().parent.parent / "deploy" / "docker-compose.prod.yml").read_text()
    assert "LOG_FORMAT: json" in compose


# --- Client errors (B32) -----------------------------------------------------------


def _value(client) -> float:
    for line in client.get("/metrics").text.splitlines():
        if line.startswith("lab_client_errors_total"):
            return float(line.split()[-1])
    raise AssertionError("lab_client_errors_total is not served")


def test_an_anonymous_client_error_is_logged_and_counted(client, caplog):
    caplog.set_level(logging.INFO, logger="lab.client")
    before = _value(client)
    resp = client.post(
        "/client-errors",
        json={"message": "x is undefined", "stack": "at a (b.js:1)", "url": "/jobs/1", "user_agent": "Mozilla"},
    )
    assert resp.status_code == 202
    assert _value(client) == before + 1
    [record] = [r for r in caplog.records if r.name == "lab.client"]
    assert record.levelno == logging.WARNING
    assert record.fields == {
        "message": "x is undefined",
        "stack": "at a (b.js:1)",
        "url": "/jobs/1",
        "user_agent": "Mozilla",
        "user": None,
    }


def test_a_signed_in_client_error_names_the_user(client, caplog):
    caplog.set_level(logging.INFO, logger="lab.client")
    email = f"ce-{uuid.uuid4().hex[:6]}@uni.edu"
    client.post("/client-errors", json={"message": "boom"}, headers={"X-Dev-User": email})
    [record] = [r for r in caplog.records if r.name == "lab.client"]
    assert record.fields["user"] == email


def test_client_error_fields_are_optional_and_long_values_are_cut(client, caplog):
    caplog.set_level(logging.INFO, logger="lab.client")
    assert client.post("/client-errors", json={}).status_code == 202
    assert client.post("/client-errors", json={"message": "m" * 5000, "stack": "s" * 20000, "url": None}).status_code == 202
    empty, long = [r.fields for r in caplog.records if r.name == "lab.client"]
    assert empty["message"] == "" and long["url"] == ""
    assert (len(long["message"]), len(long["stack"])) == (2000, 8000)


def test_client_error_counter_starts_at_zero_in_the_scrape(client):
    assert _value(client) >= 0
