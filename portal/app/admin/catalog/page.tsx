"use client";

import { useCallback, useEffect, useState } from "react";
import ConfirmButton from "@/components/ConfirmButton";
import DataTable, { type Column } from "@/components/DataTable";
import { Empty, ErrorBox, Loading } from "@/components/States";
import { api, jsonInit, type CatalogEnvironment, type Page } from "@/lib/api";
import Time from "@/components/Time";

type Form = {
  name: string;
  framework: string;
  python_version: string;
  cuda_version: string;
  gpu_archs: string;
  image_digest: string;
};

const BLANK: Form = { name: "", framework: "", python_version: "", cuda_version: "", gpu_archs: "", image_digest: "" };
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

/** "sm_89" and "8.9" mean the same architecture; the catalog keeps the compute capability form. */
function normArch(a: string) {
  const m = /^sm_(\d)(\d)$/i.exec(a.trim());
  return m ? `${m[1]}.${m[2]}` : a.trim();
}

/** Newest architecture first (numeric), "cpu" last. */
function sortArchs(archs: string[]) {
  const num = (a: string) => (a === "cpu" ? -Infinity : parseFloat(normArch(a)));
  return [...archs].sort((a, b) => {
    const x = num(a);
    const y = num(b);
    if (Number.isNaN(x) || Number.isNaN(y)) return a.localeCompare(b);
    return y - x;
  });
}

function archList(text: string) {
  return sortArchs(Array.from(new Set(text.split(",").map(normArch).filter(Boolean))));
}

export default function AdminCatalogPage() {
  const [items, setItems] = useState<CatalogEnvironment[] | null>(null);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [editing, setEditing] = useState<CatalogEnvironment | null>(null);
  const [form, setForm] = useState<Form>(BLANK);
  const [formError, setFormError] = useState("");
  const [saving, setSaving] = useState(false);
  const [showRetired, setShowRetired] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const data = (await api("/api/admin/environments")) as Page<CatalogEnvironment>;
      setItems(data.items);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load the catalog");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  function startEdit(e: CatalogEnvironment) {
    setEditing(e);
    setFormError("");
    setNote("");
    setForm({
      name: e.name,
      framework: e.framework,
      python_version: e.python_version,
      cuda_version: e.cuda_version,
      gpu_archs: sortArchs(e.gpu_archs.map(normArch)).join(", "),
      image_digest: e.image_digest,
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function reset() {
    setEditing(null);
    setForm(BLANK);
    setFormError("");
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setFormError("");
    const body = {
      name: form.name.trim(),
      framework: form.framework.trim(),
      python_version: form.python_version.trim(),
      cuda_version: form.cuda_version.trim(),
      gpu_archs: archList(form.gpu_archs),
      image_digest: form.image_digest.trim(),
    };
    if (!body.name || !NAME_RE.test(body.name)) {
      return setFormError("The name needs letters, digits, dots, dashes or underscores, and must start with a letter or digit.");
    }
    if (body.name.toLowerCase().startsWith("build-")) return setFormError('Names starting with "build-" are reserved for custom builds.');
    if (!body.framework || !body.python_version || !body.cuda_version) return setFormError("Framework, Python and CUDA versions are required.");
    if (body.gpu_archs.length === 0) return setFormError("List at least one GPU architecture, for example 8.9, 8.6, 7.5 (or cpu).");
    if (!body.image_digest) return setFormError("Enter the image reference or digest.");
    setSaving(true);
    try {
      if (editing) {
        await api(`/api/admin/environments/${editing.id}`, jsonInit("PATCH", body));
        setNote(`Saved ${body.name}.`);
      } else {
        await api("/api/admin/environments", jsonInit("POST", body));
        setNote(`Created ${body.name}.`);
      }
      reset();
      await refresh();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "could not save the image");
    } finally {
      setSaving(false);
    }
  }

  async function setRetired(e: CatalogEnvironment, retired: boolean) {
    try {
      await api(`/api/admin/environments/${e.id}`, jsonInit("PATCH", { retired }));
      setNote(retired ? `${e.name} retired. Existing jobs keep running; new submissions are refused.` : `${e.name} restored.`);
    } catch (err) {
      setNote(err instanceof Error ? err.message : "action failed");
    }
    await refresh();
  }

  const set = (k: keyof Form) => (ev: { target: { value: string } }) => setForm((f) => ({ ...f, [k]: ev.target.value }));

  const columns: Column<CatalogEnvironment>[] = [
    {
      key: "name",
      header: "Image",
      render: (e) => (
        <>
          <strong>{e.name}</strong> {e.retired ? <span className="chip retired">retired</span> : null}
          <div className="muted nodes-sub">
            {e.framework} · Python {e.python_version} · CUDA {e.cuda_version}
          </div>
          {e.retired ? <div className="muted nodes-sub"><Time iso={e.retired_at} prefix="retired " /></div> : null}
        </>
      ),
    },
    { key: "archs", header: "GPU archs", render: (e) => <span className="mono wrap-any">{sortArchs(e.gpu_archs.map(normArch)).join(", ")}</span> },
    {
      key: "image",
      header: "Image reference",
      render: (e) => (
        <>
          <div className="mono nodes-sub wrap-any" title={e.image_digest}>
            {e.image_digest}
          </div>
          {e.upstream_image ? (
            <div className="muted nodes-sub wrap-any">
              upstream: <span className="mono">{e.upstream_image}</span>
            </div>
          ) : null}
        </>
      ),
    },
    { key: "jobs", header: "Jobs, 30 days", align: "right", render: (e) => <span className="tnum">{e.jobs_last_30d}</span> },
    {
      key: "actions",
      header: "",
      render: (e) => (
        <div className="nodes-actions">
          <button type="button" className="ghost small" onClick={() => startEdit(e)}>
            Edit
          </button>
          {e.retired ? (
            <button type="button" className="ghost small" onClick={() => void setRetired(e, false)}>
              Restore
            </button>
          ) : (
            <ConfirmButton
              className="ghost danger small"
              message={`Retire ${e.name}?`}
              onConfirm={() => setRetired(e, true)}
            >
              Retire
            </ConfirmButton>
          )}
        </div>
      ),
    },
  ];

  if (items === null) {
    return (
      <div className="stack">
        <h1>Catalog</h1>
        {error ? <ErrorBox message={error} onRetry={() => void refresh()} /> : <Loading label="Loading catalog…" />}
      </div>
    );
  }

  const shown = showRetired ? items : items.filter((e) => !e.retired);

  return (
    <div className="stack">
      <div className="row">
        <div>
          <h1>Catalog</h1>
          <p className="muted">
            The images students can pick. A retired image disappears from the submit page and is refused on new jobs;
            running jobs finish. Changing the image reference to a public image makes the lab registry mirror it again.
          </p>
        </div>
        <label className="label-inline">
          <input type="checkbox" checked={showRetired} onChange={(e) => setShowRetired(e.target.checked)} />
          Show retired
        </label>
      </div>
      {error ? <ErrorBox message={`Could not refresh: ${error}`} onRetry={() => void refresh()} /> : null}
      {note ? (
        <p role="status" className="muted">
          {note}
        </p>
      ) : null}

      <form className="card stack" onSubmit={submit}>
        <div className="row">
          <h3 className="dash-h">{editing ? `Edit ${editing.name}` : "Add an image"}</h3>
          {editing ? (
            <button type="button" className="ghost small" onClick={reset}>
              Cancel edit
            </button>
          ) : null}
        </div>
        <div className="form-grid">
          <label>
            Name
            <input value={form.name} onChange={set("name")} maxLength={80} placeholder="pytorch-2.4-cu121" />
          </label>
          <label>
            Framework
            <input value={form.framework} onChange={set("framework")} maxLength={40} placeholder="pytorch" />
          </label>
          <label>
            Python version
            <input value={form.python_version} onChange={set("python_version")} maxLength={20} placeholder="3.11" />
          </label>
          <label>
            CUDA version
            <input value={form.cuda_version} onChange={set("cuda_version")} maxLength={20} placeholder="12.1" />
          </label>
          <label className="wide">
            GPU architectures (compute capability, comma separated)
            <input value={form.gpu_archs} onChange={set("gpu_archs")} placeholder="8.9, 8.6, 7.5" />
          </label>
          <label className="wide">
            Image reference or digest
            <input value={form.image_digest} onChange={set("image_digest")} maxLength={500} className="mono" placeholder="pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime" />
          </label>
        </div>
        {formError ? (
          <p className="msg-err" role="alert">
            {formError}
          </p>
        ) : null}
        <div>
          <button type="submit" disabled={saving}>
            {saving ? "Saving…" : editing ? "Save changes" : "Add image"}
          </button>
        </div>
      </form>

      <div className="card">
        <DataTable
          columns={columns}
          rows={shown}
          rowKey={(e) => e.id}
          caption="Catalog images"
          rowClassName={(e) => (e.retired ? "catalog-retired" : undefined)}
          empty={<Empty title="No images" hint="Add the first catalog image above." />}
        />
      </div>
    </div>
  );
}
