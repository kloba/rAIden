import { Table2 } from "lucide-react";
import { useMemo, useRef, useState, type FocusEvent, type MouseEvent } from "react";
import { Link } from "react-router";
import { clock, duration, titleCase } from "../lib/format";
import type { Overview, Severity } from "../lib/types";
import { cn, EnvBadge, IconButton, SeverityBadge, ServerTag } from "./ui";

const SEV_COLOR: Record<Severity, string> = { sev1: "var(--crit)", sev2: "var(--warn)", sev3: "var(--info)", sev4: "var(--ink-3)" };
const ENV_ORDER = { production: 0, staging: 1, development: 2 } as const;
const LANE = 18;

type Span = Overview["timeline"]["incidents"][number] & { a: number; b: number; lane: number };

/** Axis ticks on local clock boundaries: every hour (6 h), every 3 hours (24 h) or every midnight (7 days). */
function ticks(t0: number, t1: number): number[] {
  const span = t1 - t0;
  const hours = span <= 7 * 3600_000 ? 1 : span <= 26 * 3600_000 ? 3 : 24;
  const d = new Date(t0);
  if (hours === 24) d.setHours(0, 0, 0, 0);
  else d.setHours(d.getHours() - (d.getHours() % hours), 0, 0, 0);
  const out: number[] = [];
  for (; d.getTime() <= t1; d.setHours(d.getHours() + hours)) {
    if (d.getTime() > t0 + span * 0.02 && d.getTime() < t1 - span * 0.04) out.push(d.getTime());
  }
  return out;
}

/** The shift: one lane per server, an incident is a span from detection to resolution, and every decision is marked
 *  where it happened: amber diamonds when a person decided, violet ticks when rAIden acted on its own. */
export function ShiftTimeline({ data, now }: { data: Overview; now: number }) {
  const [table, setTable] = useState(false);
  const [hover, setHover] = useState<{ s: Span; x: number; y: number } | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const show = (s: Span) => (e: MouseEvent<HTMLElement> | FocusEvent<HTMLElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const b = box.current!.getBoundingClientRect();
    setHover({ s, x: Math.min(Math.max(r.left + r.width / 2 - b.left - 140, 0), b.width - 280), y: r.bottom - b.top + 6 });
  };
  const t0 = new Date(data.start).getTime();
  const t1 = Math.max(new Date(data.end).getTime(), now);
  const pos = (t: number) => Math.min(100, Math.max(0, ((t - t0) / (t1 - t0)) * 100));
  const servers = useMemo(
    () => [...data.timeline.servers].sort((a, b) => ENV_ORDER[a.environment] - ENV_ORDER[b.environment] || a.name.localeCompare(b.name)),
    [data.timeline.servers],
  );
  const rows = useMemo(() => {
    const by = new Map<string, Span[]>();
    for (const inc of data.timeline.incidents) {
      const a = new Date(inc.start).getTime();
      const b = inc.end ? new Date(inc.end).getTime() : t1;
      if (b < t0) continue;
      const list = by.get(inc.server_id) ?? [];
      list.push({ ...inc, a, b, lane: 0 });
      by.set(inc.server_id, list);
    }
    for (const list of by.values()) {
      list.sort((x, y) => x.a - y.a);
      const ends: number[] = [];
      for (const s of list) {
        let lane = ends.findIndex((e) => e + (t1 - t0) * 0.09 < s.a); // room for the label after a bar
        if (lane < 0) lane = ends.length < 3 ? ends.length : ends.indexOf(Math.min(...ends));
        ends[lane] = s.b;
        s.lane = lane;
      }
    }
    return by;
  }, [data.timeline.incidents, t0, t1]);
  const marks = useMemo(() => {
    const by = new Map<string, { t: number; human: boolean; incident: string }[]>();
    for (const d of data.timeline.decisions) {
      if (!d.server_id) continue;
      const human = d.kind === "decision" && !["autopilot", "raiden", "system"].includes(d.actor);
      if (d.kind === "decision" && !human) continue;
      const list = by.get(d.server_id) ?? [];
      list.push({ t: new Date(d.ts).getTime(), human, incident: d.incident_id });
      by.set(d.server_id, list);
    }
    return by;
  }, [data.timeline.decisions]);
  const tk = ticks(t0, t1);
  const multiDay = t1 - t0 > 26 * 3600_000;

  if (table) {
    const flat = [...rows.values()].flat().sort((x, y) => y.a - x.a);
    return (
      <div>
        <div className="mb-2 flex justify-end"><IconButton label="Show the timeline" className="size-7" onClick={() => setTable(false)}><Table2 className="size-3.5" /></IconButton></div>
        <div className="max-h-[360px] overflow-auto rounded-lg border border-line">
          <table className="w-full text-left text-[12.5px]">
            <thead className="sticky top-0 bg-surface-2 text-[11.5px] text-ink-3"><tr>{["Incident", "Server", "Detected", "Duration", "Stage"].map((h) => <th key={h} className="px-3 py-1.5 font-semibold">{h}</th>)}</tr></thead>
            <tbody>
              {flat.map((s) => (
                <tr key={s.id} className="border-t border-line">
                  <td className="px-3 py-1.5"><Link to={`/incidents/${s.id}`} className="inline-flex items-center gap-2 hover:underline"><SeverityBadge severity={s.severity} /><span className="line-clamp-1">{s.title}</span></Link></td>
                  <td className="px-3 py-1.5 whitespace-nowrap">{servers.find((x) => x.id === s.server_id)?.name}</td>
                  <td className="px-3 py-1.5 whitespace-nowrap num">{clock(s.start, multiDay)}</td>
                  <td className="px-3 py-1.5 whitespace-nowrap num">{s.end ? duration((s.b - s.a) / 1000) : `open ${duration((now - s.a) / 1000)}`}</td>
                  <td className="px-3 py-1.5 capitalize">{s.stage}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    );
  }

  return (
    <div className="relative" ref={box}>
      <div className="grid grid-cols-[minmax(130px,190px)_1fr] gap-x-3">
        {/* axis */}
        <div className="flex items-end justify-end pb-1">
          <IconButton label="Show as a table" className="size-6" onClick={() => setTable(true)}><Table2 className="size-3.5" /></IconButton>
        </div>
        <div className="relative h-6 border-b border-line">
          {tk.map((t) => (
            <span key={t} className="absolute bottom-1 -translate-x-1/2 text-[11px] whitespace-nowrap text-ink-3 num" style={{ left: `${pos(t)}%` }}>
              {multiDay ? new Date(t).toLocaleDateString("en-GB", { weekday: "short", day: "numeric" }) : clock(t / 1000)}
            </span>
          ))}
          <span className="absolute right-0 bottom-1 translate-x-1/2 rounded bg-arc px-1 text-[10px] font-bold tracking-wide text-on-arc uppercase">now</span>
        </div>

        {servers.map((srv) => {
          const spans = rows.get(srv.id) ?? [];
          const lanes = Math.max(1, ...spans.map((s) => s.lane + 1));
          return (
            <div key={srv.id} className="contents">
              <Link to={`/servers/${srv.id}`} className="flex min-w-0 items-center gap-2 border-b border-line/70 py-1.5 text-[12.5px] hover:underline">
                <ServerTag name={srv.name} slot={srv.color_slot} />
                {srv.environment !== "production" && <span className="hidden xl:inline"><EnvBadge env={srv.environment} /></span>}
              </Link>
              <div className="relative border-b border-line/70" style={{ height: lanes * LANE + 14 }}>
                {tk.map((t) => <span key={t} className="absolute top-0 bottom-0 w-px bg-[var(--grid)]" style={{ left: `${pos(t)}%` }} aria-hidden />)}
                {spans.map((s) => {
                  const left = pos(s.a);
                  const width = Math.max(pos(s.b) - left, 1);
                  const open = !s.end;
                  const waiting = open && s.state === "waiting";
                  const labelRight = left + width < 90;
                  return (
                    <span key={s.id} className="contents">
                    {width <= 7 && (
                      <span aria-hidden className={cn("pointer-events-none absolute text-[10.5px] leading-[14px] font-semibold whitespace-nowrap num", waiting ? "text-lamp-ink" : "text-ink-3")}
                        style={labelRight ? { left: `calc(${left + width}% + 4px)`, top: 7 + s.lane * LANE } : { right: `calc(${100 - left}% + 4px)`, top: 7 + s.lane * LANE }}>
                        {s.id}
                      </span>
                    )}
                    <Link to={`/incidents/${s.id}`}
                      aria-label={`${s.id} ${s.severity.toUpperCase()} ${s.title}, ${open ? `open, ${s.stage}` : `resolved after ${duration((s.b - s.a) / 1000)}`}`}
                      onMouseEnter={show(s)} onMouseLeave={() => setHover(null)} onFocus={show(s)} onBlur={() => setHover(null)}
                      className={cn("absolute flex items-center overflow-hidden rounded-[4px] text-[10.5px] font-semibold whitespace-nowrap text-ink transition-[filter] hover:brightness-95",
                        waiting && "ring-2 ring-lamp ring-offset-1 ring-offset-surface")}
                      style={{
                        left: `${left}%`, width: `${width}%`, top: 7 + s.lane * LANE, height: LANE - 4,
                        background: `color-mix(in srgb, ${SEV_COLOR[s.severity]} ${open ? 30 : 16}%, var(--surface))`,
                        borderLeft: `3px solid ${SEV_COLOR[s.severity]}`,
                      }}>
                      {width > 7 && <span className="truncate px-1.5">{s.id}</span>}
                      {open && <span className={cn("absolute top-1/2 right-0.5 size-1.5 -translate-y-1/2 rounded-full", waiting ? "lamp-glow bg-lamp" : "working bg-arc")} />}
                    </Link>
                    </span>
                  );
                })}
                {(marks.get(srv.id) ?? []).filter((m) => m.t >= t0).map((m, i) => (
                  <span key={i} aria-hidden className="pointer-events-none absolute top-[1px] -translate-x-1/2" style={{ left: `${pos(m.t)}%` }}>
                    {m.human ? (
                      <svg width="8" height="8" viewBox="0 0 8 8"><path d="M4 0 8 4 4 8 0 4z" fill="var(--lamp)" stroke="var(--surface)" strokeWidth="1" /></svg>
                    ) : (
                      <svg width="8" height="7" viewBox="0 0 8 7"><path d="M4 7 0 0h8z" fill="var(--arc)" stroke="var(--surface)" strokeWidth="1" /></svg>
                    )}
                  </span>
                ))}
                <span className="absolute top-0 right-0 bottom-0 w-px bg-arc/60" aria-hidden />
              </div>
            </div>
          );
        })}
      </div>

      {hover && (
        <div className="pointer-events-none absolute z-20 w-[280px] rounded-lg border border-line bg-surface px-3 py-2 shadow-pop"
          style={{ left: hover.x, top: hover.y }}>
          <div className="flex items-center gap-2 text-[11.5px]"><SeverityBadge severity={hover.s.severity} /><span className="mono text-ink-3">{hover.s.id}</span></div>
          <div className="mt-1 text-[12.5px] leading-snug font-semibold">{hover.s.title}</div>
          <div className="mt-1 text-[11.5px] text-ink-3 num">
            {clock(hover.s.start, multiDay)} → {hover.s.end ? `${clock(hover.s.end, multiDay)} · ${duration((hover.s.b - hover.s.a) / 1000)}` : `${titleCase(hover.s.stage)}${hover.s.state === "waiting" ? " · waiting for you" : ""}`}
          </div>
        </div>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[11.5px] text-ink-2">
        {(["sev1", "sev2", "sev3"] as Severity[]).map((s) => (
          <span key={s} className="inline-flex items-center gap-1.5"><span className="inline-block h-2.5 w-4 rounded-[3px]" style={{ background: `color-mix(in srgb, ${SEV_COLOR[s]} 30%, var(--surface))`, borderLeft: `3px solid ${SEV_COLOR[s]}` }} />{s.toUpperCase()}</span>
        ))}
        <span className="inline-flex items-center gap-1.5"><svg width="9" height="9" viewBox="0 0 8 8"><path d="M4 0 8 4 4 8 0 4z" fill="var(--lamp)" /></svg>A person decided</span>
        <span className="inline-flex items-center gap-1.5"><svg width="9" height="8" viewBox="0 0 8 7"><path d="M4 7 0 0h8z" fill="var(--arc)" /></svg>rAIden acted</span>
        <span className="inline-flex items-center gap-1.5"><span className="inline-block h-2.5 w-4 rounded-[3px] ring-2 ring-lamp" />Waiting for you</span>
      </div>
    </div>
  );
}

