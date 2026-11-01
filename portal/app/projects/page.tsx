"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { Empty, ErrorBox, Loading } from "@/components/States";
import { api, jsonInit, type Project } from "@/lib/api";

export default function ProjectsPage() {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [error, setError] = useState("");
  const [name, setName] = useState("");
  const [formError, setFormError] = useState("");
  const [saving, setSaving] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const data = (await api("/api/projects")) as { projects: Project[] };
      setProjects([...data.projects].sort((a, b) => a.name.localeCompare(b.name)));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "could not load projects");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) return setFormError("Enter a project name.");
    if (trimmed.length > 120) return setFormError("Project names are at most 120 characters.");
    setSaving(true);
    setFormError("");
    try {
      await api("/api/projects", jsonInit("POST", { name: trimmed }));
      setName("");
      await refresh();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "could not create the project");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="stack">
      <div>
        <h1>Projects</h1>
        <p className="muted">A project groups jobs and the people who can see them. Owners manage its members.</p>
      </div>

      <form className="card filters" onSubmit={create}>
        <label>
          New project name
          <input value={name} maxLength={120} onChange={(e) => setName(e.target.value)} placeholder="Thesis experiments" />
        </label>
        <button type="submit" disabled={saving}>
          {saving ? "Creating…" : "Create project"}
        </button>
        {formError ? (
          <p className="err" role="alert">
            {formError}
          </p>
        ) : null}
      </form>

      {projects === null ? (
        error ? (
          <ErrorBox message={error} onRetry={() => void refresh()} />
        ) : (
          <Loading label="Loading projects…" />
        )
      ) : projects.length === 0 ? (
        <div className="card">
          <Empty title="No projects yet" hint="Create one above, or ask a project owner to add you." />
        </div>
      ) : (
        <div className="card">
          {error ? <ErrorBox message={`Could not refresh: ${error}`} onRetry={() => void refresh()} /> : null}
          <ul className="list-plain">
            {projects.map((p) => (
              <li key={p.id}>
                <Link href={`/projects/${p.id}`}>{p.name}</Link>
                <span className="muted">Members and quota</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
