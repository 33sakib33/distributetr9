"""The load-test report (B31): percentiles, throughput and error counts."""
from __future__ import annotations

from harness.simulator import Stats, format_summary, percentile


def test_percentiles_use_the_nearest_rank():
    values = list(range(1, 101))
    assert (percentile(values, 50), percentile(values, 95), percentile(values, 100)) == (50, 95, 100)
    assert percentile([7.0], 95) == 7.0
    assert percentile([], 50) == 0.0
    assert percentile([5, 1, 3], 50) == 3  # unsorted input


def test_the_summary_reports_claims_throughput_and_errors():
    stats = Stats()
    for ms in (10.0, 20.0, 30.0, 400.0):
        stats.claimed(ms)
    stats.empty()
    for _ in range(30):
        stats.finished()
    stats.last_completed_at = 110.0
    stats.error("complete: 500 boom")
    summary = stats.summary(started_at=100.0, ended_at=200.0)
    assert summary["claims_with_job"] == 4 and summary["empty_claims"] == 1
    assert (summary["claim_p50_ms"], summary["claim_p95_ms"], summary["claim_max_ms"]) == (20.0, 400.0, 400.0)
    assert summary["completed_jobs"] == 30
    assert summary["jobs_per_second"] == 3.0 and summary["busy_seconds"] == 10.0  # to the last completion
    assert summary["errors"] == 1
    assert "claim_p95_ms: 400.0" in format_summary(summary)


def test_an_empty_run_reports_zeros_without_dividing_by_zero():
    summary = Stats().summary(started_at=5.0, ended_at=5.0)
    assert summary["completed_jobs"] == 0 and summary["jobs_per_second"] == 0.0 and summary["claim_max_ms"] == 0.0
