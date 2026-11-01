// Date-range helpers for the usage pages.

function isoDay(d: Date) {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** The last 30 days as local `YYYY-MM-DD` strings, today included. */
export function defaultRange() {
  const to = new Date();
  const from = new Date(to.getTime() - 30 * 86_400_000);
  return { from: isoDay(from), to: isoDay(to) };
}

/** `to` is inclusive for the user, so the API gets the start of the following day. */
export function rangeParams(from: string, to: string) {
  const end = new Date(`${to}T00:00:00`);
  end.setDate(end.getDate() + 1);
  return new URLSearchParams({
    from: new Date(`${from}T00:00:00`).toISOString(),
    to: end.toISOString(),
  });
}
