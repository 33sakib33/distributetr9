"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, type Environment, type Project } from "@/lib/api";

export default function SubmitPage() {
  const router = useRouter();
  const [projects, setProjects] = useState<Project[]>([]);
  const [envs, setEnvs] = useState<Environment[]>([]);
  const [projectId, setProjectId] = useState("");
  const [envId, setEnvId] = useState("");
  const [entrypoint, setEntrypoint] = useState("train.py");
  const [gpuClass, setGpuClass] = useState("ampere-8g");
  const [minVram, setMinVram] = useState("4096");
  const [runtime, setRuntime] = useState("14400");
  const [file, setFile] = useState<File | null>(null);
  const [requirements, setRequirements] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    Promise.all([api("/api/projects"), api("/api/environments")])
      .then(([p, e]) => {
        const projects = (p as { projects: Project[] }).projects;
        const environments = (e as { environments: Environment[] }).environments;
        setProjects(projects);
        setEnvs(environments);
        if (projects[0]) setProjectId(projects[0].id);
        const torch = environments.find((x) => x.name.includes("pytorch")) || environments[0];
        if (torch) setEnvId(torch.id);
      })
      .catch((err) => setError(err instanceof Error ? err.message : "load failed"));
  }, []);

  async function ensureProject() {
    if (projectId) return projectId;
    const created = (await api("/api/projects", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name: "demo" }),
    })) as Project;
    setProjects((prev) => [...prev, created]);
    setProjectId(created.id);
    return created.id;
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (!file) {
      setError("Choose a .tar.gz or .zip archive that contains train.py");
      return;
    }
    setBusy(true);
    try {
      const pid = await ensureProject();
      const body = new FormData();
      body.set("project_id", pid);
      body.set("entrypoint", entrypoint);
      body.set("environment_id", envId);
      if (requirements.trim()) {
        body.set("requirements", requirements);
      }
      body.set("args", "[]");
      body.set(
        "resources",
        JSON.stringify({
          gpu_class: gpuClass || null,
          min_vram_mb: Number(minVram) || 0,
          max_runtime_seconds: Number(runtime) || 3600,
        }),
      );
      body.set("archive", file);
      const job = (await api("/api/jobs", { method: "POST", body })) as { id: string };
      router.push(`/jobs/${job.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "submit failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card stack" onSubmit={onSubmit}>
      <h1>Submit</h1>
      <p className="muted">Python archive. Use a catalog image, or paste extra pip packages to build on the lab PC first.</p>
      <div className="grid2">
        <label>
          Project
          <select value={projectId} onChange={(e) => setProjectId(e.target.value)}>
            <option value="">Create “demo”</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Environment
          <select value={envId} onChange={(e) => setEnvId(e.target.value)} required>
            {envs.map((env) => (
              <option key={env.id} value={env.id}>
                {env.name} ({env.cuda_version})
              </option>
            ))}
          </select>
        </label>
        <label>
          Entrypoint
          <input value={entrypoint} onChange={(e) => setEntrypoint(e.target.value)} required />
        </label>
        <label>
          Archive (.tar.gz or .zip)
          <input type="file" accept=".gz,.tgz,.tar,.zip" onChange={(e) => setFile(e.target.files?.[0] || null)} />
        </label>
        <label>
          GPU class
          <select value={gpuClass} onChange={(e) => setGpuClass(e.target.value)}>
            <option value="">any / CPU</option>
            <option value="ampere-8g">ampere-8g (3050 / 8 GB)</option>
            <option value="ampere-12g">ampere-12g</option>
            <option value="ada-8g">ada-8g</option>
            <option value="ada-12g">ada-12g</option>
            <option value="ada-16g">ada-16g</option>
            <option value="ada-24g">ada-24g</option>
            <option value="ampere-24g">ampere-24g</option>
          </select>
        </label>
        <label>
          Min VRAM (MB)
          <input value={minVram} onChange={(e) => setMinVram(e.target.value)} />
        </label>
        <label>
          Max runtime (seconds)
          <input value={runtime} onChange={(e) => setRuntime(e.target.value)} />
        </label>
      </div>
      <label>
        Extra packages (optional requirements.txt)
        <textarea
          rows={6}
          value={requirements}
          onChange={(e) => setRequirements(e.target.value)}
          placeholder={"# leave empty to use the catalog image only\n# numpy==1.26.4"}
          style={{ width: "100%", fontFamily: "ui-monospace, monospace" }}
        />
      </label>
      {error ? <p className="err">{error}</p> : null}
      <button type="submit" disabled={busy}>
        {busy ? "Uploading…" : "Queue job"}
      </button>
    </form>
  );
}
