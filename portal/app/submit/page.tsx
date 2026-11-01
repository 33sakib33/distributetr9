"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { ErrorBox } from "@/components/States";
import {
  api,
  jsonInit,
  type Environment,
  type GpuAvailability,
  type InspectResult,
  type Job,
  type Project,
} from "@/lib/api";
import { fmtBytes, fmtDuration, fmtMb } from "@/lib/format";
import { createZip, filesFromDataTransfer, filesFromInput, type ZipFile } from "@/lib/zip";

const ARCHIVE_EXT = /\.(py|zip|tar|tgz|tar\.gz)$/i;
const SKIP_PATH = /(^|\/)(\.git|__pycache__|\.venv|venv|node_modules|\.ipynb_checkpoints)(\/|$)|(^|\/)\.DS_Store$|\.pyc$/;
const MAX_UPLOAD_BYTES = 500 * 1024 * 1024;

type Upload = { file: File; note: string };

/** Zip a folder (or several loose files), dropping caches and a lone top-level folder name. */
function zipFolder(files: ZipFile[], fallbackName: string): File {
  let kept = files.filter((f) => !SKIP_PATH.test(f.path));
  if (!kept.length) throw new Error("The folder has no files to upload.");
  let name = fallbackName || "upload";
  const tops = new Set(kept.map((f) => f.path.split("/")[0]));
  if (tops.size === 1 && kept.every((f) => f.path.includes("/"))) {
    name = Array.from(tops)[0];
    kept = kept.map((f) => ({ path: f.path.slice(name.length + 1), data: f.data }));
  }
  const total = kept.reduce((n, f) => n + f.data.length, 0);
  if (total > MAX_UPLOAD_BYTES) {
    throw new Error(
      `The folder is ${fmtBytes(total)}; the limit is ${fmtBytes(MAX_UPLOAD_BYTES)}. Leave datasets and checkpoints out.`,
    );
  }
  return new File([createZip(kept)], `${name}.zip`, { type: "application/zip" });
}

/** Split an argument line like `--epochs 5 --name "my run"` into words. */
function splitArgs(line: string): string[] {
  const out: string[] = [];
  const re = /"([^"]*)"|'([^']*)'|(\S+)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(line))) out.push(m[1] ?? m[2] ?? m[3]);
  return out;
}

function joinArgs(args: unknown[] | undefined): string {
  return (args ?? []).map((a) => (/\s/.test(String(a)) ? JSON.stringify(String(a)) : String(a))).join(" ");
}

function numberOrUndefined(text: string): number | undefined {
  const n = Number(text);
  return text.trim() && Number.isFinite(n) && n > 0 ? Math.round(n) : undefined;
}

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

function waitText(c: GpuAvailability) {
  if (c.nodes_online === 0) return null;
  if (c.nodes_free > 0) return null;
  return c.est_wait_seconds == null ? "wait unknown" : `wait ~${fmtDuration(c.est_wait_seconds)}`;
}

export default function SubmitPage() {
  const router = useRouter();
  const [cloneId, setCloneId] = useState<string | null | undefined>(undefined);
  const [source, setSource] = useState<Job | null>(null);

  const [projects, setProjects] = useState<Project[]>([]);
  const [envs, setEnvs] = useState<Environment[]>([]);
  const [classes, setClasses] = useState<GpuAvailability[] | null>(null);
  const [availError, setAvailError] = useState("");
  const [loadError, setLoadError] = useState("");

  const [projectId, setProjectId] = useState("");
  const [envId, setEnvId] = useState("");
  const [envTouched, setEnvTouched] = useState(false);
  const [entrypoint, setEntrypoint] = useState("");
  const [entrypoints, setEntrypoints] = useState<string[]>([]);
  const [gpuClass, setGpuClass] = useState("");
  const [gpuTouched, setGpuTouched] = useState(false);
  const [minVram, setMinVram] = useState("");
  const [runtimeMin, setRuntimeMin] = useState("240");
  const [cpuCores, setCpuCores] = useState("");
  const [ramMb, setRamMb] = useState("");
  const [diskMb, setDiskMb] = useState("");
  const [argsLine, setArgsLine] = useState("");
  const [requirements, setRequirements] = useState("");
  const [foundRequirements, setFoundRequirements] = useState(false);

  const [upload, setUpload] = useState<Upload | null>(null);
  const [over, setOver] = useState(false);
  const [picking, setPicking] = useState(false);
  const [inspecting, setInspecting] = useState(false);
  const [inspectNote, setInspectNote] = useState("");
  const [pickError, setPickError] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [showOffline, setShowOffline] = useState(false);
  const errorTop = useRef<HTMLDivElement>(null);

  const fileInput = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);
  const inspectSeq = useRef(0);

  // Which job to clone comes from the query string (?clone=<job id>).
  useEffect(() => {
    setCloneId(new URLSearchParams(window.location.search).get("clone"));
  }, []);

  useEffect(() => {
    Promise.all([api("/api/projects"), api("/api/environments")])
      .then(([p, e]) => {
        const list = (p as { projects: Project[] }).projects;
        setProjects(list);
        setEnvs((e as { environments: Environment[] }).environments);
        setProjectId((cur) => cur || list[0]?.id || "");
      })
      .catch((err) => setLoadError(err instanceof Error ? err.message : "Could not load projects and environments."));
  }, []);

  const loadAvailability = useCallback(async () => {
    try {
      const data = (await api("/api/availability")) as { classes: GpuAvailability[] };
      setClasses(data.classes);
      setAvailError("");
    } catch (err) {
      setAvailError(err instanceof Error ? err.message : "Could not load GPU availability.");
      setClasses((cur) => cur ?? []);
    }
  }, []);

  useEffect(() => {
    void loadAvailability();
    const t = setInterval(() => void loadAvailability(), 20000);
    return () => clearInterval(t);
  }, [loadAvailability]);

  // Prefill from the job being cloned.
  useEffect(() => {
    if (!cloneId) return;
    api(`/api/jobs/${cloneId}`)
      .then((data) => {
        const job = data as Job;
        const r = job.resources ?? {};
        setSource(job);
        setEntrypoint(job.entrypoint);
        setArgsLine(joinArgs(job.args));
        setGpuClass(typeof r.gpu_class === "string" ? r.gpu_class : "");
        setGpuTouched(true);
        if (r.min_vram_mb) setMinVram(String(r.min_vram_mb));
        if (r.max_runtime_seconds) setRuntimeMin(String(Math.max(1, Math.round(Number(r.max_runtime_seconds) / 60))));
        if (r.cpu_cores) setCpuCores(String(r.cpu_cores));
        if (r.ram_mb) setRamMb(String(r.ram_mb));
        if (r.disk_mb) setDiskMb(String(r.disk_mb));
        if (job.project_id) setProjectId(job.project_id);
      })
      .catch((err) => setLoadError(err instanceof Error ? err.message : "Could not load the job to clone."));
  }, [cloneId]);

  const gpuCards = (classes ?? [])
    .filter((c) => c.gpu_class !== "cpu" && c.vram_mb > 0)
    .sort((a, b) => a.vram_mb - b.vram_mb || a.gpu_class.localeCompare(b.gpu_class));
  // A class carried over from a clone may no longer have any PC; keep it visible but unusable.
  if (gpuClass && classes && !gpuCards.some((c) => c.gpu_class === gpuClass)) {
    gpuCards.push({
      gpu_class: gpuClass,
      gpu_model: null,
      vram_mb: 0,
      nodes_total: 0,
      nodes_online: 0,
      nodes_free: 0,
      queued: 0,
      est_wait_seconds: null,
    });
  }
  const selected = gpuCards.find((c) => c.gpu_class === gpuClass);
  const onlineCards = gpuCards.filter((c) => c.nodes_online > 0);
  const offlineCards = gpuCards.filter((c) => c.nodes_online === 0);

  // Start on the first class that has a PC online, unless the user (or a clone) chose already.
  useEffect(() => {
    if (gpuTouched || cloneId === undefined || cloneId || !classes) return;
    const first = classes
      .filter((c) => c.gpu_class !== "cpu" && c.vram_mb > 0 && c.nodes_online > 0)
      .sort((a, b) => b.nodes_free - a.nodes_free || a.vram_mb - b.vram_mb)[0];
    if (first) setGpuClass(first.gpu_class);
  }, [classes, gpuTouched, cloneId]);

  // Suggest a memory request that fits the chosen card.
  useEffect(() => {
    if (cloneId !== null) return;
    setMinVram(gpuClass && selected && selected.vram_mb > 0 ? String(Math.min(4096, selected.vram_mb)) : "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gpuClass, cloneId, selected?.vram_mb]);

  // Default environment follows the hardware until the user picks one.
  useEffect(() => {
    if (envTouched || !envs.length || cloneId === undefined || cloneId) return;
    const want = gpuClass ? "pytorch" : "cpu";
    const env = envs.find((x) => x.name.includes(want)) || envs.find((x) => x.name.includes("pytorch")) || envs[0];
    if (env) setEnvId(env.id);
  }, [envs, gpuClass, envTouched, cloneId]);

  // A failed submit is shown above the form too; bring it into view.
  useEffect(() => {
    if (error) errorTop.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [error]);

  async function inspect(file: File) {
    const seq = ++inspectSeq.current;
    setInspecting(true);
    setInspectNote("");
    try {
      const body = new FormData();
      body.set("archive", file);
      const r = (await api("/api/jobs/inspect", { method: "POST", body })) as InspectResult;
      if (seq !== inspectSeq.current) return;
      setEntrypoints(r.entrypoints);
      setEntrypoint(r.suggested_entrypoint ?? r.entrypoints[0] ?? "");
      setRequirements(r.requirements ?? "");
      setFoundRequirements(!!r.requirements);
      if (!r.entrypoints.length) setInspectNote("No .py script was found in this upload. Type the one to run.");
    } catch (err) {
      if (seq !== inspectSeq.current) return;
      setEntrypoints([]);
      setInspectNote(err instanceof Error ? err.message : "Could not read the upload.");
    } finally {
      if (seq === inspectSeq.current) setInspecting(false);
    }
  }

  function accept(file: File, note: string) {
    setPickError("");
    setError("");
    if (file.size > MAX_UPLOAD_BYTES) {
      setPickError(`That file is ${fmtBytes(file.size)}; the limit is ${fmtBytes(MAX_UPLOAD_BYTES)}.`);
      return;
    }
    setUpload({ file, note });
    void inspect(file);
  }

  function acceptFile(file: File) {
    if (!ARCHIVE_EXT.test(file.name)) {
      setPickError("Use a .py, .zip or .tar.gz file, or choose a folder.");
      return;
    }
    accept(file, `${fmtBytes(file.size)}`);
  }

  function acceptFolder(files: ZipFile[], fallbackName: string) {
    try {
      const zipped = zipFolder(files, fallbackName);
      accept(zipped, `zipped in your browser, ${fmtBytes(zipped.size)}`);
    } catch (err) {
      setPickError(err instanceof Error ? err.message : "Could not read the folder.");
    }
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    setOver(false);
    // The drop's entries are only readable inside this handler, so start reading now.
    const dropped = Array.from(e.dataTransfer.files);
    if (dropped.length === 1 && ARCHIVE_EXT.test(dropped[0].name)) {
      acceptFile(dropped[0]);
      return;
    }
    const reading = filesFromDataTransfer(e.dataTransfer.items);
    setPicking(true);
    setPickError("");
    reading
      .then((files) => {
        if (!files.length) throw new Error("Nothing readable was dropped.");
        acceptFolder(files, dropped[0]?.name ?? "upload");
      })
      .catch((err) => setPickError(err instanceof Error ? err.message : "Could not read what was dropped."))
      .finally(() => setPicking(false));
  }

  function onPickFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (file) acceptFile(file);
  }

  function onPickFolder(e: React.ChangeEvent<HTMLInputElement>) {
    const list = Array.from(e.target.files ?? []);
    e.target.value = "";
    if (!list.length) return;
    setPicking(true);
    setPickError("");
    filesFromInput(list)
      .then((files) => {
        const first = (list[0] as File & { webkitRelativePath?: string }).webkitRelativePath || "";
        acceptFolder(files, first.split("/")[0] || "upload");
      })
      .catch((err) => setPickError(err instanceof Error ? err.message : "Could not read the folder."))
      .finally(() => setPicking(false));
  }

  async function ensureProject() {
    if (projectId) return projectId;
    const created = (await api("/api/projects", jsonInit("POST", { name: "demo" }))) as Project;
    setProjects((prev) => [...prev, created]);
    setProjectId(created.id);
    return created.id;
  }

  function resourcesBody() {
    const resources: Record<string, unknown> = {
      gpu_class: gpuClass || null,
      min_vram_mb: gpuClass ? (numberOrUndefined(minVram) ?? 0) : 0,
      max_runtime_seconds: (numberOrUndefined(runtimeMin) ?? 240) * 60,
    };
    const cpu = numberOrUndefined(cpuCores);
    const ram = numberOrUndefined(ramMb);
    const disk = numberOrUndefined(diskMb);
    if (cpu) resources.cpu_cores = cpu;
    if (ram) resources.ram_mb = ram;
    if (disk) resources.disk_mb = disk;
    return resources;
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (gpuClass && selected && selected.nodes_online === 0) {
      setError(`No PC of class ${gpuClass} is online. Pick another GPU or CPU only.`);
      return;
    }
    if (cloneId) {
      await submitClone(cloneId);
      return;
    }
    if (!envId) {
      setError("Pick an environment.");
      return;
    }
    if (!upload) {
      setError("Add your code first: drop a .py file, a .zip or .tar.gz, or a folder.");
      return;
    }
    setBusy(true);
    try {
      const pid = await ensureProject();
      const body = new FormData();
      body.set("project_id", pid);
      if (entrypoint.trim()) body.set("entrypoint", entrypoint.trim());
      if (envId) body.set("environment_id", envId);
      if (requirements.trim()) body.set("requirements", requirements);
      body.set("args", JSON.stringify(splitArgs(argsLine)));
      body.set("resources", JSON.stringify(resourcesBody()));
      body.set("archive", upload.file);
      const job = (await api("/api/jobs", { method: "POST", body })) as { id: string };
      router.push(`/jobs/${job.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Submit failed.");
      setBusy(false);
    }
  }

  async function submitClone(id: string) {
    setBusy(true);
    try {
      const payload: Record<string, unknown> = {
        resources: resourcesBody(),
        args: splitArgs(argsLine),
      };
      if (entrypoint.trim() && entrypoint.trim() !== source?.entrypoint) payload.entrypoint = entrypoint.trim();
      if (envId) payload.environment_id = envId;
      const job = (await api(`/api/jobs/${id}/clone`, jsonInit("POST", payload))) as { id: string };
      router.push(`/jobs/${job.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Clone failed.");
      setBusy(false);
    }
  }

  function gpuCard(c: GpuAvailability) {
    const offline = c.nodes_online === 0;
    const wait = waitText(c);
    const title = c.gpu_model && c.gpu_model !== c.gpu_class ? c.gpu_model : c.gpu_class;
    return (
      <label key={c.gpu_class} className={`gpucard${offline ? " disabled" : ""}`}>
        <input
          type="radio"
          name="gpu"
          value={c.gpu_class}
          checked={gpuClass === c.gpu_class}
          disabled={offline}
          onChange={() => {
            setGpuClass(c.gpu_class);
            setGpuTouched(true);
          }}
        />
        <span className="gpucard-name">{title}</span>
        {title !== c.gpu_class ? <span className="gpucard-id mono">{c.gpu_class}</span> : null}
        {c.vram_mb > 0 ? <span className="muted">{fmtMb(c.vram_mb)} memory</span> : null}
        {offline ? (
          <span className="gpucard-none">No PC of this class online</span>
        ) : c.nodes_free > 0 ? (
          <span className="gpucard-free">
            {c.nodes_free === c.nodes_online
              ? `${plural(c.nodes_free, "PC", "PCs")} free now`
              : `${c.nodes_free} of ${plural(c.nodes_online, "PC", "PCs")} free now`}
          </span>
        ) : (
          <span className="gpucard-none">{c.nodes_online === 1 ? "The PC is busy" : `All ${c.nodes_online} PCs busy`}</span>
        )}
        {wait ? <span className="muted">{wait}</span> : null}
        {!offline && c.queued > 0 ? <span className="muted">{c.queued} waiting</span> : null}
      </label>
    );
  }

  const cloning = !!cloneId;
  const cpuFree = (classes ?? []).reduce((n, c) => n + c.nodes_free, 0);
  const cpuOnline = (classes ?? []).reduce((n, c) => n + c.nodes_online, 0);

  return (
    <form className="stack" onSubmit={onSubmit} noValidate>
      <div>
        <h1>{cloning ? "Clone a job" : "Submit a job"}</h1>
        <p className="muted">
          {cloning
            ? "The original upload is reused. Change anything below, then queue the copy."
            : "Add your code, pick the hardware, and queue it. You will be taken to the job page."}
        </p>
      </div>
      {loadError ? <ErrorBox message={loadError} onRetry={() => window.location.reload()} /> : null}
      <div ref={errorTop}>{error ? <ErrorBox message={error} /> : null}</div>

      {cloning ? (
        <div className="notice" role="status">
          {source ? (
            <>
              Cloning <code>{source.entrypoint}</code> from{" "}
              <Link href={`/jobs/${source.id}`}>job {source.id.slice(0, 8)}</Link>.{" "}
              <Link href="/submit">Upload different files instead</Link>
            </>
          ) : (
            "Loading the job to clone…"
          )}
        </div>
      ) : (
        <fieldset className="fieldset">
          <legend>1. Your code</legend>
          <div
            className={`dropzone${over ? " over" : ""}${upload ? " has-file" : ""}`}
            role="group"
            aria-label="Upload area"
            onDragOver={(e) => {
              e.preventDefault();
              setOver(true);
            }}
            onDragLeave={() => setOver(false)}
            onDrop={onDrop}
          >
            {upload ? (
              <div className="dropzone-file">
                <strong>{upload.file.name}</strong> <span className="muted">({upload.note})</span>
              </div>
            ) : (
              <div>Drop a <code>.py</code> file, a <code>.zip</code> or <code>.tar.gz</code>, or a whole folder here</div>
            )}
            {picking ? <div role="status">Reading files…</div> : null}
            <div className="dropzone-actions">
              <button type="button" className="ghost" onClick={() => fileInput.current?.click()}>
                {upload ? "Choose another file" : "Choose a file"}
              </button>
              <button type="button" className="ghost" onClick={() => folderInput.current?.click()}>
                Choose a folder
              </button>
            </div>
            <input
              ref={fileInput}
              type="file"
              className="sr-only"
              tabIndex={-1}
              aria-label="Choose a .py, .zip or .tar.gz file"
              accept=".py,.zip,.tar,.tgz,.gz"
              onChange={onPickFile}
            />
            <input
              ref={(el) => {
                folderInput.current = el;
                el?.setAttribute("webkitdirectory", "");
              }}
              type="file"
              className="sr-only"
              tabIndex={-1}
              aria-label="Choose a folder"
              onChange={onPickFolder}
            />
          </div>
          {pickError ? (
            <p className="field-error" role="alert">
              {pickError}
            </p>
          ) : null}
        </fieldset>
      )}

      <fieldset className="fieldset">
        <legend>{cloning ? "1. What to run" : "2. What to run"}</legend>
        <div className="grid2">
          <label>
            Script to run
            {entrypoints.length > 0 ? (
              <select value={entrypoint} onChange={(e) => setEntrypoint(e.target.value)}>
                {entrypoints.includes(entrypoint) ? null : <option value={entrypoint}>{entrypoint}</option>}
                {entrypoints.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
            ) : (
              <input
                value={entrypoint}
                onChange={(e) => setEntrypoint(e.target.value)}
                placeholder="train.py"
                autoComplete="off"
              />
            )}
          </label>
          <label>
            Arguments (optional)
            <input
              value={argsLine}
              onChange={(e) => setArgsLine(e.target.value)}
              placeholder="--epochs 5 --lr 0.001"
              autoComplete="off"
            />
          </label>
        </div>
        {inspecting ? <p className="muted" role="status">Looking inside your upload…</p> : null}
        {inspectNote ? <p className="hint">{inspectNote}</p> : null}
        {cloning ? null : (
          <label>
            Extra packages (requirements.txt)
            <textarea
              className="codebox"
              rows={5}
              value={requirements}
              onChange={(e) => setRequirements(e.target.value)}
              placeholder={"# leave empty to use the catalog image only\n# numpy==1.26.4"}
            />
          </label>
        )}
        {foundRequirements && !cloning ? (
          <p className="muted">
            Found a requirements.txt in your upload. These packages are installed in a build step before the job runs.
            Clear the box to use the catalog image only.
          </p>
        ) : null}
      </fieldset>

      <fieldset className="fieldset">
        <legend>{cloning ? "2. Hardware" : "3. Hardware"}</legend>
        <div className="gpucards" role="radiogroup" aria-label="GPU">
          {onlineCards.map((c) => gpuCard(c))}
          {showOffline || offlineCards.some((c) => c.gpu_class === gpuClass) ? offlineCards.map((c) => gpuCard(c)) : null}
          <label className="gpucard">
            <input
              type="radio"
              name="gpu"
              value=""
              checked={gpuClass === ""}
              onChange={() => {
                setGpuClass("");
                setGpuTouched(true);
              }}
            />
            <span className="gpucard-name">CPU only</span>
            <span className="muted">No GPU, runs on any lab PC</span>
            {classes && cpuOnline > 0 ? (
              <span className={cpuFree > 0 ? "gpucard-free" : "gpucard-none"}>
                {cpuFree > 0 ? `${plural(cpuFree, "PC", "PCs")} free now` : cpuOnline === 1 ? "The PC is busy" : "All PCs busy"}
              </span>
            ) : null}
          </label>
        </div>
        {offlineCards.length > 0 ? (
          <div className="offline-toggle">
            <button
              type="button"
              className="ghost small"
              aria-expanded={showOffline}
              onClick={() => setShowOffline((v) => !v)}
            >
              {showOffline ? "Hide offline classes" : `Show offline classes (${offlineCards.length})`}
            </button>
          </div>
        ) : null}
        {availError ? <ErrorBox message={availError} onRetry={() => void loadAvailability()} /> : null}
        {classes && !availError && gpuCards.length === 0 ? (
          <p className="muted">No lab PC with a GPU has registered yet. CPU only still works.</p>
        ) : null}

        <div className="grid2" style={{ marginTop: 14 }}>
          <label>
            Project
            <select
              value={projectId}
              onChange={(e) => setProjectId(e.target.value)}
              disabled={cloning}
            >
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
            <select
              value={envId}
              onChange={(e) => {
                setEnvId(e.target.value);
                setEnvTouched(true);
              }}
              required={!cloning}
            >
              {cloning ? <option value="">Same as the original job</option> : null}
              {envs.map((env) => (
                <option key={env.id} value={env.id}>
                  {env.name} ({env.cuda_version})
                </option>
              ))}
            </select>
          </label>
        </div>
      </fieldset>

      <details className="details-box">
        <summary>Advanced: runtime, CPU, memory, disk</summary>
        <div className="form-grid">
          <label>
            Max runtime (minutes)
            <input type="number" min={1} value={runtimeMin} onChange={(e) => setRuntimeMin(e.target.value)} />
          </label>
          <label>
            Min GPU memory (MB)
            <input
              type="number"
              min={0}
              value={minVram}
              onChange={(e) => setMinVram(e.target.value)}
              disabled={!gpuClass}
            />
          </label>
          <label>
            CPU cores
            <input type="number" min={1} value={cpuCores} onChange={(e) => setCpuCores(e.target.value)} placeholder="1" />
          </label>
          <label>
            RAM (MB)
            <input type="number" min={1} value={ramMb} onChange={(e) => setRamMb(e.target.value)} placeholder="512" />
          </label>
          <label>
            Disk (MB)
            <input type="number" min={1} value={diskMb} onChange={(e) => setDiskMb(e.target.value)} placeholder="1024" />
          </label>
        </div>
      </details>

      {error ? <p className="msg-err">{error}</p> : null}
      <div className="btn-row">
        <button type="submit" disabled={busy || picking || (cloning && !source)}>
          {busy ? (cloning ? "Queueing…" : "Uploading…") : cloning ? "Queue copy" : "Queue job"}
        </button>
        <Link className="btn ghost" href="/">
          Cancel
        </Link>
      </div>
    </form>
  );
}
