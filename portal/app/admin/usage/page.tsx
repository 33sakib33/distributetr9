"use client";

import { useCallback, useEffect, useState } from "react";
import DataTable, { type Column } from "@/components/DataTable";
import { Empty, ErrorBox, Loading } from "@/components/States";
import { api, type UsageReport, type UsageRow } from "@/lib/api";
import { defaultRange, rangeParams } from "@/lib/range";

type Group = "user" | "project" | "class";
const GROUP_LABEL: Record<Group, string> = { user: "User", project: "Project", class: "GPU class" };

export default function AdminUsagePage() {
  const initial = defaultRange();
  const [from, setFrom] = useState(initial.from);
  const [to, setTo] = useState(initial.to);
  const [group, setGroup] = useState<Group>("user");
  const [report, setReport] = useState<UsageReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const valid = Boolean(from && to && from <= to);
  const query = (extra?: Record<string, string>) => {
    const q = rangeParams(from, to);
    q.set("group", group);
    for (const [k, v] of Object.entries(extra ?? {})) q.set(k, v);
    return q.toString();
  };

  const load = useCallback(async () => {
    if (!valid) {
      setError("Choose a start date on or before the end date.");
      return;
    }
    setLoading(true);
    try {
      const q = rangeParams(from, to);
      q.set("group", group);
      setReport((await api(`/api/admin/usage?${q}`)) as UsageReport);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to load usage");
    } finally {
      setLoading(false);
    }
  }, [from, to, group, valid]);

  useEffect(() => {
    void load();
  }, [load]);

  const columns: Column<UsageRow>[] = [
    { key: "label", header: GROUP_LABEL[group], render: (r) => r.label || r.key },
    { key: "gpu", header: "GPU hours", align: "right", render: (r) => r.gpu_hours.toFixed(2) },
    { key: "jobs", header: "Jobs", align: "right", render: (r) => r.jobs },
    { key: "ok", header: "Succeeded", align: "right", render: (r) => r.succeeded },
    { key: "bad", header: "Failed", align: "right", render: (r) => r.failed },
  ];
  const lab = report?.lab;

  return (
    <div className="stack">
      <div>
        <h1>Lab usage</h1>
        <p className="muted">Admin only. GPU hours are job run time on GPU nodes in the chosen period.</p>
      </div>
      <div className="toolbar">
        <label>
          Group by
          <select value={group} onChange={(e) => setGroup(e.target.value as Group)}>
            <option value="user">User</option>
            <option value="project">Project</option>
            <option value="class">GPU class</option>
          </select>
        </label>
        <label>
          From
          <input type="date" value={from} max={to} onChange={(e) => setFrom(e.target.value)} />
        </label>
        <label>
          To
          <input type="date" value={to} min={from} onChange={(e) => setTo(e.target.value)} />
        </label>
        {valid ? (
          <a className="btn ghost" href={`/api/admin/usage?${query({ format: "csv" })}`} download>
            Download CSV
          </a>
        ) : null}
      </div>
      {error ? <ErrorBox message={error} onRetry={() => void load()} /> : null}
      {loading && !report ? <Loading label="Loading usage…" /> : null}
      {report ? (
        <>
          {lab ? (
            <div className="card">
              <div className="stats">
                <div>
                  <div className="stat-label">Lab utilisation</div>
                  <div className="stat-value">{Math.round(lab.utilisation * 100)}%</div>
                </div>
                <div>
                  <div className="stat-label">Online hours</div>
                  <div className="stat-value">{lab.online_hours.toFixed(1)}</div>
                </div>
                <div>
                  <div className="stat-label">Busy hours</div>
                  <div className="stat-value">{lab.busy_hours.toFixed(1)}</div>
                </div>
              </div>
            </div>
          ) : null}
          <div className="card" style={{ padding: 0 }}>
            <DataTable
              columns={columns}
              rows={report.rows}
              rowKey={(r) => r.key}
              loading={loading}
              caption={`Usage by ${GROUP_LABEL[group].toLowerCase()}`}
              empty={<Empty title="No usage in this period" hint="Try a longer date range." />}
            />
          </div>
        </>
      ) : null}
    </div>
  );
}
