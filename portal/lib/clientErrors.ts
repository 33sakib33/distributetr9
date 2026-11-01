/** Report browser errors to the control plane (POST /client-errors, B32). */

const WINDOW_MS = 60_000;
const MAX_PER_WINDOW = 3;

let sent: number[] = [];
const seen = new Set<string>();

type Report = { message: string; stack?: string | null; url?: string };

/**
 * Send one error report. At most MAX_PER_WINDOW a minute per tab, and the same
 * message+stack is sent once, so a render loop cannot flood the server.
 * Never throws: reporting must not cause another error.
 */
export function reportClientError(report: Report): void {
  try {
    const message = String(report.message || "unknown error").slice(0, 2000);
    const stack = String(report.stack || "").slice(0, 8000);
    const key = `${message}\n${stack}`;
    if (seen.has(key)) return;
    const now = Date.now();
    sent = sent.filter((t) => now - t < WINDOW_MS);
    if (sent.length >= MAX_PER_WINDOW) return;
    sent.push(now);
    seen.add(key);
    void fetch("/api/client-errors", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        message,
        stack,
        url: report.url || window.location.href,
        user_agent: navigator.userAgent,
      }),
      keepalive: true,
    }).catch(() => undefined);
  } catch {
    // Nothing useful to do if reporting itself fails.
  }
}
