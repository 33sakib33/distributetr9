from __future__ import annotations

import json

from tests.test_scheduler_races import isolated


def _sse_events(text: str) -> list[tuple[str, dict | None, str | None]]:
    events = []
    current_event = "message"
    current_id = None
    data_lines: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if line == "":
            if data_lines:
                payload = "\n".join(data_lines)
                parsed = None
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    parsed = None
                events.append((current_event, parsed, current_id))
            current_event = "message"
            current_id = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            current_event = line.split(":", 1)[1].strip()
        elif line.startswith("id:"):
            current_id = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].lstrip())
    return events


def test_sse_replays_logs_and_honors_last_event_id(client):
    portal, agent, job_id = isolated(client)
    attempt = agent.claim()
    assert attempt is not None
    agent.acknowledge(attempt)
    agent.event(attempt, "running")
    posted = agent.post_logs(attempt, ["hello", "world"])
    assert posted["status_code"] == 200, posted
    agent.event(attempt, "uploading")
    done = agent.complete(attempt)
    assert done["status_code"] == 200, done

    with client.stream("GET", f"/jobs/{job_id}/logs/stream", headers=portal.headers) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = "".join(resp.iter_text())
    events = _sse_events(body)
    logs = [e for e in events if e[0] == "log"]
    assert [e[1]["line"] for e in logs] == ["hello", "world"]
    assert logs[0][2] == "1"
    assert any(e[0] == "done" and e[1]["status"] == "completed" for e in events)

    reconnect = {**portal.headers, "Last-Event-ID": "1"}
    with client.stream("GET", f"/jobs/{job_id}/logs/stream", headers=reconnect) as resp:
        body = "".join(resp.iter_text())
    events = _sse_events(body)
    logs = [e for e in events if e[0] == "log"]
    assert [e[1]["line"] for e in logs] == ["world"]
    assert logs[0][2] == "2"
