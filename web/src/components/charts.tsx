import { Table2 } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { Area, Bar, BarChart, CartesianGrid, ComposedChart, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { axisValue, clock, compact, value as fmtValue } from "../lib/format";
import type { MetricDef } from "../lib/types";
import { cn, IconButton } from "./ui";

export interface LineSeries {
  key: string;
  label: string;
  color: string;
  points: [number, number][];
}

function merge(series: LineSeries[]): Record<string, number>[] {
  const rows = new Map<number, Record<string, number>>();
  for (const s of series) {
    for (const [t, v] of s.points) {
      const r = rows.get(t) ?? { t };
      r[s.key] = v;
      rows.set(t, r);
    }
  }
  return [...rows.values()].sort((a, b) => a.t! - b.t!);
}

function tickTime(start: number, end: number) {
  const span = end - start;
  return (t: number) => {
    const d = new Date(t * 1000);
    if (span > 3 * 86400) return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
    return d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  };
}

function ChartTooltip({ active, payload, label, def, series, span }: any) {
  if (!active || !payload?.length) return null;
  const byKey = new Map(series.map((s: LineSeries) => [s.key, s]));
  const items = [...payload].filter((p: any) => p.value != null).sort((a: any, b: any) => b.value - a.value);
  return (
    <div className="min-w-[170px] rounded-lg border border-line bg-surface px-3 py-2 shadow-pop">
      <div className="mb-1 text-[11.5px] text-ink-3 num">{span > 3 * 86400 ? clock(label, true) : new Date(label * 1000).toLocaleTimeString("en-GB")}</div>
      {items.map((p: any) => {
        const s = byKey.get(p.dataKey) as LineSeries | undefined;
        return (
          <div key={p.dataKey} className="flex items-center justify-between gap-4 text-[12.5px]">
            <span className="flex min-w-0 items-center gap-1.5 text-ink-2">
              <span className="inline-block h-[2px] w-3 shrink-0 rounded" style={{ background: s?.color }} />
              <span className="truncate">{s?.label ?? p.dataKey}</span>
            </span>
            <span className="font-semibold text-ink num">{fmtValue(def, p.value)}</span>
          </div>
        );
      })}
    </div>
  );
}

export function Legend({ series }: { series: { key: string; label: string; color: string }[] }) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-ink-2">
      {series.map((s) => (
        <span key={s.key} className="inline-flex items-center gap-1.5">
          <span className="inline-block h-[2px] w-3.5 rounded" style={{ background: s.color }} />
          {s.label}
        </span>
      ))}
    </div>
  );
}

/** Markers in the visible range, and whether each one's label fits without running into the one before it (near the right
 * edge a label sits left of its line). When one does not fit, a legend under the chart names them all instead. */
function placeMarkers(markers: { t: number; label: string; color: string }[] | undefined, start: number, end: number) {
  const width = 0.17; // a label's share of the plot: about 55 px on a narrow card
  let taken = -1;
  return (markers ?? []).filter((m) => m.t >= start && m.t <= end).sort((a, b) => a.t - b.t).map((m) => {
    const x = (m.t - start) / (end - start || 1);
    const right = x > 0.75;
    const show = (right ? x - width : x) >= taken;
    if (show) taken = right ? x : x + width;
    return { ...m, x, right, show };
  });
}

/** A time-series chart: one axis, thin lines, hairline grid, thresholds, crosshair tooltip, legend for 2+ series, table view. */
export function TimeChart({ series, def, start, end, height = 180, thresholds = true, area, extra, loading, yMax, markers, refLines }: {
  series: LineSeries[]; def?: MetricDef; start: number; end: number; height?: number; thresholds?: boolean; area?: boolean; extra?: ReactNode;
  loading?: boolean; yMax?: number; markers?: { t: number; label: string; color: string }[]; refLines?: { y: number; label: string; color: string }[];
}) {
  const [table, setTable] = useState(false);
  const rows = useMemo(() => merge(series), [series]);
  const empty = rows.length === 0;
  const max = Math.max(0, ...rows.flatMap((r) => series.map((s) => r[s.key] ?? 0)));
  const showWarn = thresholds && def?.warn != null && !def.lower_is_worse && def.warn <= max * 1.6;
  const showCrit = thresholds && def?.crit != null && !def.lower_is_worse && def.crit <= max * 1.6;
  const domainMax = yMax ?? (def?.unit === "percent" ? (max > 60 ? 100 : "auto") : max <= 1 ? 1 : "auto");  // 0/1 signals get 0 and 1, not 0 to 4
  const single = series.length === 1;
  const placed = placeMarkers(markers, start, end);
  const labelled = placed.every((m) => m.show);
  return (
    <div className={cn("relative transition-opacity", loading && "opacity-55")}>
      <div className="mb-1 flex min-h-5 items-center justify-between gap-2">
        {series.length > 1 ? <Legend series={series} /> : <span />}
        <div className="flex items-center gap-1">
          {extra}
          <IconButton label={table ? "Show chart" : "Show as table"} className="size-6" onClick={() => setTable((v) => !v)}><Table2 className="size-3.5" /></IconButton>
        </div>
      </div>
      {table ? (
        <div className="overflow-auto rounded-lg border border-line" style={{ maxHeight: height + 20 }}>
          <table className="w-full text-[12px]">
            <thead><tr><th className="sticky top-0 bg-surface-2 px-2 py-1 text-left text-ink-3">Time</th>{series.map((s) => <th key={s.key} className="sticky top-0 bg-surface-2 px-2 py-1 text-right text-ink-3">{s.label}</th>)}</tr></thead>
            <tbody>{[...rows].reverse().slice(0, 120).map((r) => (
              <tr key={r.t} className="border-t border-line/60"><td className="px-2 py-0.5 text-ink-3 num">{clock(r.t!, end - start > 86400)}</td>
                {series.map((s) => <td key={s.key} className="px-2 py-0.5 text-right num">{fmtValue(def, r[s.key])}</td>)}</tr>
            ))}</tbody>
          </table>
        </div>
      ) : empty ? (
        <div className="flex items-center justify-center rounded-lg border border-dashed border-line text-[12.5px] text-ink-3" style={{ height }}>
          {loading ? "Loading…" : "No data in this range"}
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={height}>
          <ComposedChart data={rows} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid vertical={false} stroke="var(--grid)" />
            <XAxis dataKey="t" type="number" domain={[start, end]} tickFormatter={tickTime(start, end)} stroke="var(--axis)" tick={{ fill: "var(--ink-3)", fontSize: 11 }}
              tickLine={false} axisLine={{ stroke: "var(--axis)" }} minTickGap={42} />
            <YAxis width={46} tickFormatter={(v) => axisValue(def, v)} stroke="var(--axis)" tick={{ fill: "var(--ink-3)", fontSize: 11 }} tickLine={false} axisLine={false}
              domain={[0, domainMax as any]} allowDecimals={false} />
            <Tooltip content={<ChartTooltip def={def} series={series} span={end - start} />} cursor={{ stroke: "var(--ink-3)", strokeWidth: 1 }} isAnimationActive={false} />
            {showWarn && <ReferenceLine y={def!.warn!} stroke="var(--warn)" strokeDasharray="4 4" strokeOpacity={0.7} ifOverflow="extendDomain" />}
            {showCrit && <ReferenceLine y={def!.crit!} stroke="var(--crit)" strokeDasharray="4 4" strokeOpacity={0.7} ifOverflow="extendDomain" />}
            {refLines?.map((r) => (
              <ReferenceLine key={`${r.label}-${r.y}`} y={r.y} stroke={r.color} strokeDasharray="4 4" strokeWidth={1.5} ifOverflow="extendDomain"
                label={{ value: r.label, position: "insideTopRight", fill: "var(--ink-2)", fontSize: 10.5, fontWeight: 600 }} />
            ))}
            {placed.map((m) => (
              <ReferenceLine key={`${m.label}-${m.t}`} x={m.t} stroke={m.color} strokeWidth={1.5}
                label={labelled ? { value: m.label, position: m.right ? "insideTopRight" : "insideTopLeft", fill: "var(--ink-2)", fontSize: 10.5, fontWeight: 600 } : undefined} />
            ))}
            {series.map((s) =>
              (area ?? single) ? (
                <Area key={s.key} type="monotone" dataKey={s.key} stroke={s.color} strokeWidth={2} fill={s.color} fillOpacity={0.1} dot={false} isAnimationActive={false} connectNulls />
              ) : (
                <Line key={s.key} type="monotone" dataKey={s.key} stroke={s.color} strokeWidth={2} dot={false} isAnimationActive={false} connectNulls />
              ),
            )}
          </ComposedChart>
        </ResponsiveContainer>
      )}
      {!table && !empty && !labelled && (
        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 pl-12 text-[11px] text-ink-3">
          {placed.map((m) => (
            <span key={`${m.label}-${m.t}`} className="inline-flex items-center gap-1"><span className="inline-block h-3 w-[2px]" style={{ background: m.color }} />{m.label}</span>
          ))}
        </div>
      )}
    </div>
  );
}

export function Sparkline({ values, color = "var(--ink-3)", width = 96, height = 26, warn }: { values: number[]; color?: string; width?: number; height?: number; warn?: boolean }) {
  if (!values || values.length < 2) return <svg width={width} height={height} aria-hidden />;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const pts = values.map((v, i) => [(i / (values.length - 1)) * (width - 4) + 2, height - 3 - ((v - min) / span) * (height - 6)] as const);
  const d = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
  const last = pts[pts.length - 1]!;
  return (
    <svg width={width} height={height} aria-hidden className="overflow-visible">
      <path d={`${d} L${last[0]},${height} L2,${height} Z`} fill={warn ? "var(--warn)" : color} fillOpacity={0.08} />
      <path d={d} fill="none" stroke={warn ? "var(--warn)" : color} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={last[0]} cy={last[1]} r={2.5} fill={warn ? "var(--warn)" : color} stroke="var(--surface)" strokeWidth={1.5} />
    </svg>
  );
}

/** A meter: the fill carries severity, the track is a lighter step of the same ramp. */
export function Meter({ value, max, label, sub }: { value: number; max: number | null; label?: ReactNode; sub?: ReactNode }) {
  const pct = max ? Math.min(100, (100 * value) / max) : 0;
  const tone = pct >= 90 ? "var(--crit)" : pct >= 75 ? "var(--warn)" : "var(--arc)";
  return (
    <div>
      {(label || sub) && (
        <div className="mb-1 flex items-baseline justify-between gap-2 text-[12px]">
          <span className="text-ink-2">{label}</span>
          <span className="text-ink-3 num">{sub}</span>
        </div>
      )}
      <div className="h-2 overflow-hidden rounded-full" style={{ background: "color-mix(in srgb, " + tone + " 16%, transparent)" }}>
        <div className="h-full rounded-full transition-all" style={{ width: max ? `${Math.max(pct, value > 0 ? 2 : 0)}%` : "0%", background: tone }} />
      </div>
    </div>
  );
}

/** Horizontal bars: one per server, for the estate summaries (CPU, memory, storage...). */
export function BarList({ items, def, max = 100 }: { items: { key: string; label: ReactNode; value: number; color?: string }[]; def?: MetricDef; max?: number }) {
  return (
    <div className="space-y-2">
      {items.map((it) => {
        const health = def ? (def.crit != null && it.value >= def.crit ? "var(--crit)" : def.warn != null && it.value >= def.warn ? "var(--warn)" : it.color ?? "var(--arc)") : it.color ?? "var(--arc)";
        return (
          <div key={it.key} className="grid grid-cols-[minmax(0,140px)_1fr_auto] items-center gap-3 text-[12.5px]">
            <div className="min-w-0 truncate">{it.label}</div>
            <div className="h-1.5 rounded-full bg-surface-3">
              <div className="h-full rounded-full" style={{ width: `${Math.min(100, (100 * it.value) / max)}%`, background: health }} />
            </div>
            <div className="w-12 text-right font-semibold num">{fmtValue(def, it.value)}</div>
          </div>
        );
      })}
    </div>
  );
}

/** Stacked bars per day (token usage): 2px surface gaps between segments, legend above, values in the tooltip. */
export function StackedBars({ data, keys, height = 200, format = compact }: { data: Record<string, any>[]; keys: { key: string; label: string; color: string }[]; height?: number; format?: (v: number) => string }) {
  return (
    <div>
      <div className="mb-2"><Legend series={keys} /></div>
      <ResponsiveContainer width="100%" height={height}>
        <BarChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: 0 }} barCategoryGap="22%">
          <CartesianGrid vertical={false} stroke="var(--grid)" />
          <XAxis dataKey="day" tickFormatter={(d) => new Date(d).toLocaleDateString("en-GB", { day: "numeric", month: "short" })} tick={{ fill: "var(--ink-3)", fontSize: 11 }}
            tickLine={false} axisLine={{ stroke: "var(--axis)" }} minTickGap={24} />
          <YAxis width={46} tickFormatter={(v) => format(v)} tick={{ fill: "var(--ink-3)", fontSize: 11 }} tickLine={false} axisLine={false} />
          <Tooltip cursor={{ fill: "var(--surface-3)", opacity: 0.6 }} isAnimationActive={false}
            content={({ active, payload, label }: any) => active && payload?.length ? (
              <div className="min-w-[180px] rounded-lg border border-line bg-surface px-3 py-2 shadow-pop">
                <div className="mb-1 text-[11.5px] text-ink-3">{new Date(label).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" })}</div>
                {[...payload].reverse().map((p: any) => (
                  <div key={p.dataKey} className="flex items-center justify-between gap-4 text-[12.5px]">
                    <span className="flex items-center gap-1.5 text-ink-2"><span className="inline-block size-2 rounded-sm" style={{ background: p.color }} />{keys.find((k) => k.key === p.dataKey)?.label}</span>
                    <span className="font-semibold num">{format(p.value)}</span>
                  </div>
                ))}
              </div>) : null} />
          {keys.map((k, i) => (
            <Bar key={k.key} dataKey={k.key} stackId="a" fill={k.color} stroke="var(--surface)" strokeWidth={1} maxBarSize={24} isAnimationActive={false}
              radius={i === keys.length - 1 ? [4, 4, 0, 0] : [0, 0, 0, 0]} />
          ))}
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
