"use client";

import { useEffect, useId, useRef, useState } from "react";

export type Point = { t: number; v: number | null };
export type Series = { name: string; points: Point[]; color?: string };

const PALETTE = ["var(--run)", "var(--accent)", "var(--ok)", "var(--warn)", "var(--bad)"];
const DEFAULT_W = 480;
const M = { left: 52, right: 12, top: 12, bottom: 26 };

function hhmm(t: number) {
  // t is epoch seconds (milliseconds are detected by magnitude).
  const d = new Date(t > 1e11 ? t : t * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/** A round step (1, 2, 5 times a power of ten) near `raw`. */
function niceStep(raw: number) {
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const n = raw / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
}

/**
 * The y range and tick values for the data. With `fixedMax` the axis is 0..max
 * (percentages, total memory). Otherwise it follows min..max with padding and
 * round ticks, never going below zero when the data does not.
 */
function scaleFor(min: number, max: number, fixedMax?: number) {
  if (fixedMax !== undefined) return { lo: 0, hi: fixedMax, ticks: [0, fixedMax / 2, fixedMax] };
  let lo = min;
  let hi = max;
  if (hi - lo < 1e-9) {
    const pad = Math.max(Math.abs(hi) * 0.1, 1);
    lo -= pad;
    hi += pad;
  } else {
    const pad = (hi - lo) * 0.1;
    lo -= pad;
    hi += pad;
  }
  if (min >= 0 && lo < 0) lo = 0;
  const step = niceStep((hi - lo) / 4);
  lo = Math.floor(lo / step) * step;
  hi = Math.ceil(hi / step) * step;
  const ticks: number[] = [];
  for (let v = lo; v <= hi + step / 2; v += step) ticks.push(Number(v.toFixed(10)));
  return { lo, hi, ticks };
}

function fmtTick(v: number) {
  if (Number.isInteger(v)) return String(v);
  return Math.abs(v) < 10 ? v.toFixed(1) : String(Math.round(v));
}

/** Width of the element in pixels, kept current while it resizes. */
function useWidth() {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(DEFAULT_W);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const read = () => setWidth(Math.max(240, Math.round(el.getBoundingClientRect().width)) || DEFAULT_W);
    read();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width] as const;
}

/** Dependency-free SVG line chart. Null values break the line; width follows the container. */
export default function LineChart({
  series,
  yMax,
  yLabel,
  unit = "",
  height = 180,
}: {
  series: Series[];
  yMax?: number;
  yLabel?: string;
  unit?: string;
  height?: number;
}) {
  const uid = useId();
  const [box, W] = useWidth();
  const all = series.flatMap((s) => s.points.filter((p) => p.v !== null && Number.isFinite(p.v)));
  if (!all.length) {
    return (
      <div className="linechart" ref={box}>
        {yLabel ? <div className="linechart-ylabel">{yLabel}</div> : null}
        <div className="linechart-empty" role="status">
          No data yet
        </div>
      </div>
    );
  }

  const times = series.flatMap((s) => s.points.map((p) => p.t));
  const tMin = Math.min(...times);
  const tMax = Math.max(...times);
  const dataMax = Math.max(...all.map((p) => p.v as number));
  const dataMin = Math.min(...all.map((p) => p.v as number));
  const { lo, hi, ticks } = scaleFor(dataMin, dataMax, yMax);
  const span = hi - lo || 1;
  const iw = W - M.left - M.right;
  const ih = height - M.top - M.bottom;
  const x = (t: number) => M.left + (tMax === tMin ? iw / 2 : ((t - tMin) / (tMax - tMin)) * iw);
  const y = (v: number) => M.top + ih - ((Math.min(Math.max(v, lo), hi) - lo) / span) * ih;

  const titleId = `${uid}-t`;
  const descId = `${uid}-d`;
  const names = series.map((s) => s.name).join(", ");
  const summary = series
    .map((s) => {
      const vs = s.points.filter((p) => p.v !== null).map((p) => p.v as number);
      if (!vs.length) return `${s.name}: no data`;
      return `${s.name}: latest ${fmtTick(vs[vs.length - 1])}${unit}, max ${fmtTick(Math.max(...vs))}${unit}`;
    })
    .join("; ");

  return (
    <div className="linechart" ref={box}>
      {yLabel ? <div className="linechart-ylabel">{yLabel}</div> : null}
      <svg
        width={W}
        height={height}
        viewBox={`0 0 ${W} ${height}`}
        role="img"
        aria-labelledby={`${titleId} ${descId}`}
      >
        <title id={titleId}>{yLabel ? `${yLabel} over time` : `${names} over time`}</title>
        <desc id={descId}>
          {hhmm(tMin)} to {hhmm(tMax)}. {summary}.
        </desc>
        {ticks.map((tv) => (
          <g key={tv}>
            <line className="linechart-grid" x1={M.left} x2={W - M.right} y1={y(tv)} y2={y(tv)} />
            <text className="linechart-tick" x={M.left - 6} y={y(tv) + 4} textAnchor="end">
              {fmtTick(tv)}
              {unit}
            </text>
          </g>
        ))}
        <line className="linechart-axis" x1={M.left} x2={W - M.right} y1={M.top + ih} y2={M.top + ih} />
        <text className="linechart-tick" x={M.left} y={height - 7} textAnchor="start">
          {hhmm(tMin)}
        </text>
        {tMax !== tMin ? (
          <text className="linechart-tick" x={W - M.right} y={height - 7} textAnchor="end">
            {hhmm(tMax)}
          </text>
        ) : null}
        {series.map((s, i) => {
          const color = s.color ?? PALETTE[i % PALETTE.length];
          const sorted = [...s.points].sort((a, b) => a.t - b.t);
          const segs: Point[][] = [];
          let cur: Point[] = [];
          for (const p of sorted) {
            if (p.v === null || !Number.isFinite(p.v)) {
              if (cur.length) segs.push(cur);
              cur = [];
            } else cur.push(p);
          }
          if (cur.length) segs.push(cur);
          return (
            <g key={s.name} stroke={color} fill={color}>
              {segs.map((seg, j) =>
                seg.length === 1 ? (
                  <circle key={j} cx={x(seg[0].t)} cy={y(seg[0].v as number)} r={2.5} stroke="none" />
                ) : (
                  <polyline
                    key={j}
                    className="linechart-line"
                    fill="none"
                    points={seg.map((p) => `${x(p.t).toFixed(1)},${y(p.v as number).toFixed(1)}`).join(" ")}
                  />
                ),
              )}
            </g>
          );
        })}
      </svg>
      {series.length > 1 || series[0]?.name ? (
        <div className="linechart-legend">
          {series.map((s, i) => (
            <span key={s.name}>
              <span
                className="linechart-swatch"
                style={{ background: s.color ?? PALETTE[i % PALETTE.length] }}
                aria-hidden="true"
              />
              {s.name}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}
