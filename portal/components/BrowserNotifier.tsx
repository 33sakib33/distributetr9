"use client";

import { useEffect } from "react";
import { api, type Job } from "@/lib/api";
import { browserNotifyEnabled } from "@/lib/browserNotify";

const FINAL = ["completed", "failed", "cancelled"];
const POLL_MS = 15000;

/**
 * Renders nothing. While this tab is open and the user opted in (Settings),
 * polls the job list and shows a notification when a job they were watching
 * finishes. The first poll after switching on only records what is running.
 */
export default function BrowserNotifier() {
  useEffect(() => {
    let seen: Map<string, string> | null = null;
    let stopped = false;

    async function tick() {
      if (!browserNotifyEnabled()) {
        seen = null;
        return;
      }
      try {
        const data = (await api("/api/jobs")) as { jobs: Job[] };
        if (stopped) return;
        const next = new Map(data.jobs.map((j) => [j.id, j.status]));
        if (seen) {
          for (const job of data.jobs) {
            const before = seen.get(job.id);
            if (before && !FINAL.includes(before) && FINAL.includes(job.status)) announce(job);
          }
        }
        seen = next;
      } catch {
        // Offline or signed out: try again next time.
      }
    }

    void tick();
    const timer = setInterval(() => void tick(), POLL_MS);
    const onChange = () => {
      seen = null;
      void tick();
    };
    window.addEventListener("lab-notify-changed", onChange);
    return () => {
      stopped = true;
      clearInterval(timer);
      window.removeEventListener("lab-notify-changed", onChange);
    };
  }, []);

  return null;
}

function announce(job: Job) {
  const verb = job.status === "completed" ? "finished" : job.status === "failed" ? "failed" : "was cancelled";
  try {
    // The tag makes several open tabs show one notification per job.
    const n = new Notification(`Job ${verb}`, { body: job.entrypoint, tag: `job-${job.id}` });
    n.onclick = () => {
      window.focus();
      window.location.href = `/jobs/${job.id}`;
      n.close();
    };
  } catch {
    // Some browsers only allow notifications from a service worker.
  }
}
