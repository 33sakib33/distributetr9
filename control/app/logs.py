"""Log ingest limits. Over limit: keep a single warning line, do not 429 the agent."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.config import settings

WARNING_LINE = "log truncated: rate or size limit exceeded"


def _as_dict(chunk: Any) -> dict:
    if hasattr(chunk, "model_dump"):
        return chunk.model_dump()
    return dict(chunk)


def select_log_chunks(attempt, chunks: list[Any], *, now: datetime | None = None) -> tuple[list[dict], dict]:
    """Pick chunks that fit rate/size/chunk caps. Returns (accepted, updated usage)."""
    now = now or datetime.now(timezone.utc)
    usage = dict(attempt.resource_usage) if isinstance(attempt.resource_usage, dict) else {}
    try:
        window_ts = float(usage["log_window_start"]) if usage.get("log_window_start") is not None else now.timestamp()
    except (TypeError, ValueError):
        window_ts = now.timestamp()
    count = int(usage.get("log_window_count") or 0)
    if now.timestamp() - window_ts >= 1:
        window_ts = now.timestamp()
        count = 0

    already = bool(usage.get("log_truncated"))
    accepted: list[dict] = []
    dropped = 0
    log_bytes = int(attempt.log_bytes or 0)
    last_seq = int(attempt.last_log_seq or 0)
    incoming = [_as_dict(c) for c in chunks[: max(0, settings.log_chunk_lines)]]
    dropped += max(0, len(chunks) - len(incoming))

    max_in_seq = last_seq
    first_dropped_seq: int | None = None
    for chunk in incoming:
        seq = int(chunk.get("seq") or 0)
        max_in_seq = max(max_in_seq, seq)
        encoded = json.dumps(chunk) + "\n"
        extra = len(encoded.encode())
        if count >= settings.log_lines_per_second or log_bytes + extra > settings.log_bytes_per_attempt:
            dropped += 1
            if first_dropped_seq is None:
                first_dropped_seq = seq
            continue
        accepted.append(chunk)
        log_bytes += extra
        count += 1

    if dropped and not already and first_dropped_seq is not None:
        warning = {
            "seq": first_dropped_seq,
            "t": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "stream": "stderr",
            "line": WARNING_LINE,
        }
        extra = len((json.dumps(warning) + "\n").encode())
        if log_bytes + extra <= settings.log_bytes_per_attempt:
            accepted.append(warning)
            log_bytes += extra
        usage["log_truncated"] = True
    elif dropped:
        usage["log_truncated"] = True

    usage["log_window_start"] = window_ts
    usage["log_window_count"] = count
    usage["last_seq"] = max(max_in_seq, last_seq)
    usage["dropped"] = dropped
    return accepted, usage
