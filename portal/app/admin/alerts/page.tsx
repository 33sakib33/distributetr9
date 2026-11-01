"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import ConfirmButton from "@/components/ConfirmButton";
import { Empty, ErrorBox, Loading } from "@/components/States";
import { api, type AlertGroup, type AlertList, type AlertRow } from "@/lib/api";
import Time from "@/components/Time";

const JSON_HEADERS = { "content-type": "application/json" };
const MUTES = [
  { minutes: 15, label: "15 minutes" },
  { minutes: 60, label: "1 hour" },
  { minutes: 24 * 60, label: "1 day" },
];

function StatusText({ a }: { a: AlertRow }) {
  if (a.status === "muted" && a.muted_until) return <Time iso={a.muted_until} prefix="muted, ends " />;
  if (a.status === "acknowledged") return <>acknowledged{a.acknowledged_by ? ` by ${a.acknowledged_by}` : ""}</>;
  if (a.status === "resolved") return <Time iso={a.resolved_at} prefix="resolved " />;
  return <>open</>;
}

export default function AdminAlertsPage() {
  const [groups, setGroups] = useState<AlertGroup[] | null>(null);
  const [showResolved, setShowResolved] = useState(false);
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const data = (await api(`/api/admin/alerts${showResolved ? "?include_resolved=1" : ""}`)) as AlertList;
      setGroups(data.groups);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "admin required");
    }
  }, [showResolved]);

  useEffect(() => {
    void refresh();
    const t = setInterval(() => void refresh(), 10000);
    return () => clearInterval(t);
  }, [refresh]);

  async function post(path: string, body?: unknown) {
    try {
      await api(path, {
        method: "POST",
        ...(body === undefined ? {} : { headers: JSON_HEADERS, body: JSON.stringify(body) }),
      });
      setActionError("");
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "action failed");
    }
    await refresh();
  }

  const ack = (a: AlertRow) => post(`/api/admin/alerts/${a.id}/ack`);
  const mute = (a: AlertRow, minutes: number) => post(`/api/admin/alerts/${a.id}/mute`, { minutes });
  const ackKind = (kind: string) => post("/api/admin/alerts/ack-kind", { kind });
  const forget = (nodeId: string) => post(`/api/nodes/${nodeId}/forget`);

  if (groups === null) {
    return (
      <div className="stack">
        <h1>Alerts</h1>
        {error ? <ErrorBox message={error} onRetry={() => void refresh()} /> : <Loading label="Loading alerts…" />}
      </div>
    );
  }

  return (
    <div className="stack">
      <div className="row">
        <div>
          <h1>Alerts</h1>
          <p className="muted">
            Acknowledge an alert to stop notifications about it until it resolves and returns. Muting silences it for a
            while.
          </p>
        </div>
        <label className="label-inline">
          <input type="checkbox" checked={showResolved} onChange={(e) => setShowResolved(e.target.checked)} />
          Show resolved
        </label>
      </div>
      {error ? <ErrorBox message={`Could not refresh: ${error}`} onRetry={() => void refresh()} /> : null}
      {actionError ? (
        <p className="msg-err" role="alert">
          {actionError}
        </p>
      ) : null}

      {groups.length === 0 ? (
        <div className="card">
          <Empty
            title={showResolved ? "No alerts in the last day" : "No open alerts"}
            hint="Everything the lab is watching for is quiet."
          />
        </div>
      ) : (
        groups.map((g) => (
          <section key={g.kind} className="card alert-group stack" aria-label={`${g.kind} alerts`}>
            <div className="row">
              <h3>
                <span className={`chip sev-${g.severity}`}>{g.severity}</span> {g.kind.replace(/_/g, " ")}{" "}
                <span className="muted">
                  ({g.count} open{g.unhandled ? `, ${g.unhandled} unhandled` : ""})
                </span>
              </h3>
              {g.unhandled > 0 ? (
                <ConfirmButton
                  className="ghost small"
                  message={`Acknowledge all ${g.unhandled}?`}
                  onConfirm={() => ackKind(g.kind)}
                >
                  Acknowledge all
                </ConfirmButton>
              ) : null}
            </div>
            <div>
              {g.alerts.map((a) => (
                <div key={a.id} className={`alert-item ${a.status}`}>
                  <div className="alert-item-main">
                    <span className={`chip ${a.status}`}>{a.status}</span> {a.message}
                    <div className="muted alert-item-meta">
                      <StatusText a={a} /> · <Time iso={a.first_seen} prefix="first seen " /> ·{" "}
                      <Time iso={a.last_seen} prefix="last seen " />
                      {a.node_id ? (
                        <>
                          {" "}
                          · <Link href={`/admin/nodes/${a.node_id}`}>node</Link>
                        </>
                      ) : null}
                    </div>
                  </div>
                  {a.status === "resolved" ? null : (
                    <div className="alert-item-actions">
                      {a.status === "open" || a.status === "muted" ? (
                        <button type="button" className="ghost small" onClick={() => void ack(a)}>
                          Acknowledge
                        </button>
                      ) : null}
                      <select
                        aria-label={`Mute alert: ${a.message}`}
                        value=""
                        onChange={(e) => {
                          if (e.target.value) void mute(a, Number(e.target.value));
                        }}
                      >
                        <option value="">Mute…</option>
                        {MUTES.map((m) => (
                          <option key={m.minutes} value={m.minutes}>
                            {m.label}
                          </option>
                        ))}
                      </select>
                      {a.kind === "offline" && a.node_id ? (
                        <details className="danger-menu danger-zone">
                          <summary>More</summary>
                          <ConfirmButton
                            className="ghost danger small"
                            message="Forget this PC? Its credential is revoked."
                            onConfirm={() => forget(a.node_id as string)}
                          >
                            Forget node
                          </ConfirmButton>
                        </details>
                      ) : null}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </section>
        ))
      )}
    </div>
  );
}
