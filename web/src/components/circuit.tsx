import { useState } from "react";
import type { Mode, Stage } from "../lib/types";
import { cn, Modal, Button } from "./ui";

/** A knife switch: closed = rAIden carries on (current flows), open = a person approves first (amber lamp). */
export function Breaker({ mode, onToggle, disabled, label, size = 30 }: { mode: Mode; onToggle?: () => void; disabled?: boolean; label: string; size?: number }) {
  const closed = mode === "auto";
  const h = Math.round(size * 0.62);
  return (
    <button
      type="button"
      role="switch"
      aria-checked={closed}
      aria-label={label}
      title={label}
      disabled={disabled || !onToggle}
      onClick={onToggle}
      className={cn("group relative inline-flex items-center justify-center rounded-md transition-colors", onToggle && !disabled && "hover:bg-surface-3", "disabled:cursor-default")}
      style={{ width: size + 8, height: h + 10 }}
    >
      <svg width={size} height={h} viewBox="0 0 30 18" aria-hidden className="overflow-visible">
        <circle cx={4} cy={13} r={2.6} fill={closed ? "var(--arc)" : "var(--ink-3)"} />
        <circle cx={26} cy={13} r={2.6} fill={closed ? "var(--arc)" : "var(--ink-3)"} />
        <line x1={4} y1={13} x2={closed ? 26 : 22} y2={closed ? 13 : 1.5} stroke={closed ? "var(--arc)" : "var(--lamp)"} strokeWidth={2.6} strokeLinecap="round"
          style={{ transition: "all 220ms cubic-bezier(.3,1.4,.6,1)" }} />
        {!closed && <circle cx={26} cy={3} r={2.8} fill="var(--lamp)" className="lamp-glow" />}
      </svg>
    </button>
  );
}

export interface CircuitStage {
  id: Stage;
  title: string;
  mode?: Mode;           // undefined: no gate (Detected)
  count: number;
  waiting: number;
  autoText?: string;
  humanText?: string;
}

/** The header of the incident board: one bus bar across all stages; each gated stage has its breaker. */
export function CircuitHeader({ stages, onToggle, canEdit, envLabel }: { stages: CircuitStage[]; onToggle: (stage: Stage, mode: Mode) => void; canEdit: boolean; envLabel: string }) {
  const [confirm, setConfirm] = useState<CircuitStage | null>(null);
  const flip = (s: CircuitStage) => {
    if (!s.mode) return;
    const next: Mode = s.mode === "auto" ? "human" : "auto";
    if (next === "auto" && envLabel === "Production") setConfirm(s);
    else onToggle(s.id, next);
  };
  return (
    <>
      <div className="grid gap-3" style={{ gridTemplateColumns: `repeat(${stages.length}, minmax(176px, 1fr))` }}>
        {stages.map((s, i) => {
          const gated = !!s.mode;
          const closed = s.mode !== "human";
          const lastClosed = i === stages.length - 1;
          return (
            <div key={s.id} className="relative pt-1">
              {/* the bus segment of this stage */}
              <svg className="absolute top-[17px] left-0 h-3 w-[calc(100%+12px)]" preserveAspectRatio="none" viewBox="0 0 100 4" aria-hidden>
                <line x1={gated ? (closed ? 0 : 20) : 0} x2={lastClosed ? 100 : 100} y1={2} y2={2} stroke={closed ? "var(--arc)" : "var(--line-strong)"} strokeWidth={closed ? 1.4 : 1.2}
                  vectorEffect="non-scaling-stroke" />
                {closed && <line x1={gated ? 0 : 0} x2={100} y1={2} y2={2} stroke="var(--arc-ink)" strokeWidth={2} vectorEffect="non-scaling-stroke" className="current" strokeOpacity={0.9} />}
              </svg>
              <div className="relative flex items-center gap-1">
                {gated ? (
                  <span className="relative -ml-1 rounded-md bg-bg">
                    <Breaker mode={s.mode!} disabled={!canEdit} onToggle={canEdit ? () => flip(s) : undefined}
                      label={`${s.title}: ${s.mode === "auto" ? s.autoText ?? "autopilot" : s.humanText ?? "a person approves"}${canEdit ? " (click to switch)" : ""}`} />
                  </span>
                ) : (
                  <span className="relative ml-0.5 inline-flex size-[22px] items-center justify-center rounded-full bg-bg"><span className="size-3 rounded-full border-2 border-arc bg-surface" /></span>
                )}
              </div>
              <div className="mt-1.5 flex items-baseline justify-between gap-2 px-0.5">
                <div className="flex items-baseline gap-2">
                  <span className="display text-[15px] tracking-wide text-ink uppercase">{s.title}</span>
                  <span className="text-[12px] font-semibold text-ink-3 num">{s.count}</span>
                </div>
                {gated && (
                  <span className={cn("text-[11px] font-bold tracking-wide uppercase", s.mode === "human" ? "text-lamp-ink" : "text-arc-ink")}>
                    {s.mode === "human" ? "Approval" : "Autopilot"}
                  </span>
                )}
              </div>
              <div className="mt-0.5 min-h-[30px] px-0.5 text-[11.5px] leading-snug text-ink-3">
                {gated ? (s.mode === "human" ? s.humanText : s.autoText) : "Monitors, routines, chat and host agents open incidents here."}
              </div>
            </div>
          );
        })}
      </div>
      <Modal open={!!confirm} onClose={() => setConfirm(null)} title={`Autopilot for ${confirm?.title} on production?`}
        footer={<>
          <Button variant="ghost" onClick={() => setConfirm(null)}>Keep approval</Button>
          <Button variant="arc" onClick={() => { if (confirm) onToggle(confirm.id, "auto"); setConfirm(null); }}>Switch to autopilot</Button>
        </>}>
        <p className="text-[13.5px] text-ink-2">{confirm?.autoText} High-risk actions (dropping a replication slot, rolling back a prepared transaction, free SQL) still wait for a person.</p>
      </Modal>
    </>
  );
}
