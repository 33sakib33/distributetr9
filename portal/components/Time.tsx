import { fmtRelative, fmtTime, parseIso } from "@/lib/format";

/** A timestamp as relative text ("5 min ago"); hover or long-press shows the full local time. */
export default function Time({ iso, prefix = "" }: { iso: string | null | undefined; prefix?: string }) {
  if (!iso || Number.isNaN(parseIso(iso))) return <>—</>;
  return (
    <time dateTime={new Date(parseIso(iso)).toISOString()} title={fmtTime(iso)}>
      {prefix}
      {fmtRelative(iso)}
    </time>
  );
}
