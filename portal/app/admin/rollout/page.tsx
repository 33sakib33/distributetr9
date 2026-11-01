"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { Empty, ErrorBox, Loading } from "@/components/States";
import { api, jsonInit, type Rollout } from "@/lib/api";
import { RINGS, ringLabel } from "@/lib/rings";

const VERSION_RE = /^\d+(\.\d+)*$/;

type Form = { target: string; ring: string; min: string };

function formOf(r: Rollout): Form {
  return {
    target: r.overrides.agent_target_version ?? "",
    ring: r.overrides.agent_rollout_ring == null ? "" : String(r.overrides.agent_rollout_ring),
    min: r.overrides.agent_min_version ?? "",
  };
}

function Setting({
  name,
  value,
  override,
  fallback,
  hint,
}: {
  name: string;
  value: string;
  override: boolean;
  fallback: string;
  hint: string;
}) {
  return (
    <div>
      <div className="setting-name">{name}</div>
      <div className="setting-value mono">{value}</div>
      <div className="setting-src muted">
        {override ? `Set here. Environment default: ${fallback}.` : `Environment default (nothing set here).`}
      </div>
      <div className="setting-src muted">{hint}</div>
    </div>
  );
}

export default function AdminRolloutPage() {
  const [data, setData] = useState<Rollout | null>(null);
  const [form, setForm] = useState<Form>({ target: "", ring: "", min: "" });
  const [error, setError] = useState("");
  const [formError, setFormError] = useState("");
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = (await api("/api/admin/rollout")) as Rollout;
      setData(r);
      setError("");
      return r;
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load rollout settings");
      return null;
    }
  }, []);

  useEffect(() => {
    void load().then((r) => r && setForm(formOf(r)));
  }, [load]);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!data) return;
    setFormError("");
    const target = form.target.trim();
    const min = form.min.trim();
    if (target && !VERSION_RE.test(target)) return setFormError("The target version must look like 1.2.3.");
    if (min && !VERSION_RE.test(min)) return setFormError("The minimum version must look like 1.2.3.");

    const before = formOf(data);
    const body: Record<string, string | number | null> = {};
    if (target !== before.target) body.target_version = target || null;
    if (min !== before.min) body.min_version = min || null;
    if (form.ring !== before.ring) body.rollout_ring = form.ring === "" ? null : Number(form.ring);
    if (Object.keys(body).length === 0) return setFormError("Nothing changed.");

    setSaving(true);
    try {
      const r = (await api("/api/admin/rollout", jsonInit("PUT", body))) as Rollout;
      setData(r);
      setForm(formOf(r));
      setNote("Rollout settings saved. PCs pick them up on their next heartbeat.");
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "could not save");
    } finally {
      setSaving(false);
    }
  }

  if (data === null) {
    return (
      <div className="stack">
        <h1>Rollout</h1>
        {error ? <ErrorBox message={error} onRetry={() => void load().then((r) => r && setForm(formOf(r)))} /> : <Loading label="Loading rollout…" />}
      </div>
    );
  }

  const versions = Object.entries(data.nodes_by_version);
  const total = versions.reduce((n, [, c]) => n + c, 0);
  const o = data.overrides;
  const bundleMismatch = data.target_version && data.bundle_version && data.target_version !== data.bundle_version;

  return (
    <div className="stack">
      <div>
        <h1>Rollout</h1>
        <p className="muted">
          Which agent version the lab PCs should run and how far the update has spread. PCs update when their ring is at
          or below the rollout ring. Set a ring for each PC on the <Link href="/admin/nodes">Nodes</Link> page.
        </p>
      </div>
      {error ? <ErrorBox message={`Could not refresh: ${error}`} onRetry={() => void load()} /> : null}
      {note ? (
        <p role="status" className="msg-ok">
          {note}
        </p>
      ) : null}

      <div className="card stack">
        <div className="setting-grid">
          <Setting
            name="Target version"
            value={data.target_version ?? "none"}
            override={!!o.agent_target_version}
            fallback={data.defaults.target_version ?? "the bundle on disk"}
            hint={`Bundle on the server: ${data.bundle_version ?? "none"}`}
          />
          <Setting
            name="Rollout ring"
            value={ringLabel(data.rollout_ring)}
            override={o.agent_rollout_ring != null}
            fallback={ringLabel(data.defaults.rollout_ring)}
            hint="PCs in this ring or a lower one update."
          />
          <Setting
            name="Minimum version"
            value={data.min_version ?? "none"}
            override={!!o.agent_min_version}
            fallback={data.defaults.min_version ?? "none"}
            hint="Older agents are marked outdated and stop claiming jobs."
          />
        </div>
        {bundleMismatch ? (
          <p className="hint">
            The target {data.target_version} is not the bundle on the server ({data.bundle_version}), so PCs cannot
            fetch it until that bundle is deployed.
          </p>
        ) : null}
      </div>

      <form className="card stack" onSubmit={save}>
        <h3 className="dash-h">Change settings</h3>
        <p className="muted">Leave a field empty to fall back to the environment default.</p>
        <div className="form-grid">
          <label>
            Target version
            <input
              value={form.target}
              onChange={(e) => setForm({ ...form, target: e.target.value })}
              placeholder={data.defaults.target_version ?? "bundle version"}
              className="mono"
            />
          </label>
          <label>
            Rollout ring
            <select value={form.ring} onChange={(e) => setForm({ ...form, ring: e.target.value })}>
              <option value="">Default ({ringLabel(data.defaults.rollout_ring)})</option>
              {RINGS.map((r) => (
                <option key={r} value={r}>
                  {ringLabel(r)}
                </option>
              ))}
            </select>
          </label>
          <label>
            Minimum version
            <input
              value={form.min}
              onChange={(e) => setForm({ ...form, min: e.target.value })}
              placeholder={data.defaults.min_version ?? "none"}
              className="mono"
            />
          </label>
        </div>
        <div className="btn-row">
          <button type="submit" disabled={saving}>
            {saving ? "Saving…" : "Save settings"}
          </button>
          {formError ? (
            <span className="msg-err" role="alert">
              {formError}
            </span>
          ) : null}
        </div>
      </form>

      <div className="card stack">
        <h3 className="dash-h">Agent versions across the fleet</h3>
        {versions.length === 0 ? (
          <Empty title="No PCs registered" />
        ) : (
          <div className="version-bars">
            {versions.map(([version, count]) => (
              <div key={version} className="version-bar">
                <span className="mono">
                  {version || "unknown"}
                  {version === data.target_version ? " (target)" : ""}
                </span>
                <div className="version-bar-track" role="img" aria-label={`${count} of ${total} PCs`}>
                  <div className="version-bar-fill" style={{ width: `${(count / total) * 100}%` }} />
                </div>
                <span className="tnum">{count}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
