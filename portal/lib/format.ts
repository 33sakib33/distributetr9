// Small formatting helpers shared by pages and components.

export function fmtBytes(n: number) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MiB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(1)} GiB`;
}

/** Seconds as "45s", "3m 05s", "2h 10m", "1d 4h". Null/NaN gives an em dash. */
export function fmtDuration(seconds: number | null | undefined) {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (s < 3600) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  const h = Math.floor(s / 3600);
  if (s < 86400) return `${h}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
}

/**
 * Epoch milliseconds of an ISO timestamp, NaN when it cannot be read. Python
 * writes microseconds ("...:05.123456+00:00"), which Safari refuses, so the
 * fraction is cut to 3 digits; a timestamp without a zone is taken as UTC.
 */
export function parseIso(s: string | null | undefined): number {
  if (!s) return NaN;
  let text = String(s).trim();
  text = text.replace(/\.(\d+)/, (_m, d: string) => `.${d.slice(0, 3).padEnd(3, "0")}`);
  if (/T\d{2}:\d{2}/.test(text) && !/(Z|[+-]\d{2}(:?\d{2})?)$/i.test(text)) text += "Z";
  return new Date(text).getTime();
}

/** ISO timestamp in the viewer's locale, e.g. "1 Nov 2026, 14:05". */
export function fmtTime(iso: string | null | undefined) {
  if (!iso) return "—";
  const ms = parseIso(iso);
  if (Number.isNaN(ms)) return "—";
  return new Date(ms).toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** "just now", "5 min ago", "3 h ago", "2 d ago"; future times read "in 5 min". */
export function fmtRelative(iso: string | null | undefined, now: number = Date.now()) {
  if (!iso) return "—";
  const t = parseIso(iso);
  if (Number.isNaN(t)) return "—";
  const diff = Math.round((now - t) / 1000);
  const abs = Math.abs(diff);
  let text: string;
  if (abs < 45) return "just now";
  if (abs < 3600) text = `${Math.max(1, Math.round(abs / 60))} min`;
  else if (abs < 86400) text = `${Math.round(abs / 3600)} h`;
  else text = `${Math.round(abs / 86400)} d`;
  return diff >= 0 ? `${text} ago` : `in ${text}`;
}

/** Megabytes as "512 MB" or "6.0 GB" (1 GB = 1024 MB, matching GPU memory reporting). */
export function fmtMb(mb: number | null | undefined) {
  if (mb == null || !Number.isFinite(mb)) return "—";
  if (mb < 1024) return `${Math.round(mb)} MB`;
  return `${(mb / 1024).toFixed(1)} GB`;
}

/** 1 -> "1st", 2 -> "2nd", 11 -> "11th", 23 -> "23rd". */
export function ordinal(n: number) {
  const m100 = n % 100;
  if (m100 >= 11 && m100 <= 13) return `${n}th`;
  return `${n}${["th", "st", "nd", "rd"][n % 10 > 3 ? 0 : n % 10] ?? "th"}`;
}
