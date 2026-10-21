import { useMutation, useQueryClient } from "@tanstack/react-query";
import { BellRing, Plus, Search, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { TimeChart } from "../components/charts";
import {
  Badge, Button, Card, cn, Drawer, EmptyState, ErrorNote, Field, Input, Loading, PageHeader, Segmented, Select, SeverityBadge, ServerTag, Switch, Textarea, useToast,
} from "../components/ui";
import { api } from "../lib/api";
import { categoryLabel, monitorRule, titleCase } from "../lib/format";
import { serverColor, useCatalog, useFleetMetric, useMe, useMetricDefs, useMonitors, useServers } from "../lib/hooks";
import type { Env, Monitor, Severity } from "../lib/types";

const OPS = { ">": "above", ">=": "at or above", "<": "below", "<=": "at or below" } as const;
const ACTION: Record<Monitor["action"], { label: string; hint: string }> = {
  investigate: { label: "Open an incident", hint: "rAIden investigates under the autonomy policy" },
  notify: { label: "Notify only", hint: "Sends the notification webhooks, no incident" },
  suggest: { label: "Add a suggestion", hint: "Shows on the server, no incident" },
};

const ruleText = (m: Pick<Monitor, "aggregation" | "metric" | "operator" | "threshold" | "window_minutes" | "dimension">, defOf: ReturnType<typeof useMetricDefs>) =>
  monitorRule(m, defOf(m.metric));


type Draft = Omit<Monitor, "id" | "key" | "builtin" | "created_by" | "firing" | "created_at"> & { id?: number; builtin?: boolean };
const blank: Draft = { name: "", description: "", metric: "cpu_percent", dimension: null, aggregation: "avg", operator: ">", threshold: 90, window_minutes: 10, severity: "sev3",
  category: "performance", scope: {}, action: "investigate", runbook: "", enabled: true };

function Editor({ draft, onClose }: { draft: Draft; onClose: () => void }) {
  const [d, setD] = useState<Draft>(draft);
  const set = (p: Partial<Draft>) => setD((x) => ({ ...x, ...p }));
  const catalog = useCatalog();
  const defOf = useMetricDefs();
  const def = defOf(d.metric);
  const preview = useFleetMetric(d.metric, "24h", []);
  const qc = useQueryClient();
  const toast = useToast();
  const save = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = { name: d.name, description: d.description, aggregation: d.aggregation, operator: d.operator, threshold: Number(d.threshold), window_minutes: Number(d.window_minutes),
        severity: d.severity, scope: d.scope, action: d.action, runbook: d.runbook, enabled: d.enabled };
      if (!d.id) Object.assign(body, { metric: d.metric, dimension: d.dimension || null, category: d.category });
      return d.id ? api.patch(`/monitors/${d.id}`, body) : api.post("/monitors", body);
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["monitors"] }); toast(d.id ? "Monitor saved." : "Monitor created: it is evaluated from the next collection."); onClose(); },
  });
  const breaches = useMemo(() => {
    const cmp = (v: number) => (d.operator === ">" ? v > d.threshold : d.operator === ">=" ? v >= d.threshold : d.operator === "<" ? v < d.threshold : v <= d.threshold);
    return (preview.data?.series ?? []).filter((s) => s.points.some(([, v]) => cmp(v))).map((s) => s.name);
  }, [preview.data, d.operator, d.threshold]);
  const envs = d.scope.environments ?? [];
  return (
    <Drawer open onClose={onClose} width={680} title={d.id ? d.name : "New monitor"} sub={d.builtin ? "Built-in: you can tune or switch it off, not delete it." : "Evaluated on every collection, for every server in scope."}
      footer={<><Button variant="ghost" onClick={onClose}>Cancel</Button><Button variant="primary" loading={save.isPending} disabled={d.name.length < 2} onClick={() => save.mutate()}>{d.id ? "Save monitor" : "Create monitor"}</Button></>}>
      <div className="space-y-4">
        <Field label="Name"><Input value={d.name} onChange={(e) => set({ name: e.target.value })} placeholder="Checkout database CPU" /></Field>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Metric" hint={d.id ? "The metric of an existing monitor cannot change" : def?.description}>
            <Select value={d.metric} disabled={!!d.id} onChange={(e) => set({ metric: e.target.value, category: defOf(e.target.value)?.category === "host" ? "performance" : d.category })}>
              {(catalog.data?.categories ?? []).map((c) => (
                <optgroup key={c.id} label={c.name}>
                  {(catalog.data?.metrics ?? []).filter((m) => m.category === c.id).map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
                </optgroup>
              ))}
            </Select>
          </Field>
          {def?.dimension ? (
            <Field label={`Only this ${def.dimension}`} hint="Empty: the total"><Input value={d.dimension ?? ""} disabled={!!d.id} onChange={(e) => set({ dimension: e.target.value || null })} /></Field>
          ) : <div />}
        </div>
        <div className="grid gap-3 sm:grid-cols-4">
          <Field label="Aggregation">
            <Select value={d.aggregation} onChange={(e) => set({ aggregation: e.target.value as Draft["aggregation"] })}>
              <option value="avg">Average</option><option value="max">Maximum</option><option value="min">Minimum</option><option value="last">Latest</option>
            </Select>
          </Field>
          <Field label="Condition">
            <Select value={d.operator} onChange={(e) => set({ operator: e.target.value as Draft["operator"] })}>
              {Object.entries(OPS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </Select>
          </Field>
          <Field label={`Threshold${def?.unit && def.unit !== "count" ? ` (${def.unit.replace(/_/g, " ")})` : ""}`}><Input type="number" value={d.threshold} onChange={(e) => set({ threshold: Number(e.target.value) })} /></Field>
          <Field label="Over (minutes)"><Input type="number" min={1} value={d.window_minutes} onChange={(e) => set({ window_minutes: Number(e.target.value) })} /></Field>
        </div>
        <div className="rounded-xl border border-line p-3">
          <div className="mb-1 flex items-baseline justify-between gap-2 text-[12.5px]">
            <span className="font-semibold">{ruleText(d, defOf)}</span>
            <span className={breaches.length ? "text-warn" : "text-ink-3"}>{breaches.length ? `Crossed in the last 24 h on ${breaches.join(", ")}` : "Not crossed in the last 24 h"}</span>
          </div>
          <TimeChart series={(preview.data?.series ?? []).map((s) => ({ key: s.server_id, label: s.name, color: serverColor(s.color_slot), points: s.points }))} def={def}
            start={preview.data?.start ?? 0} end={preview.data?.end ?? 0} height={170} area={false} thresholds={false} loading={preview.isFetching && !preview.data}
            refLines={[{ y: Number(d.threshold), label: "threshold", color: "var(--crit)" }]} />
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Severity">
            <Segmented value={d.severity} onChange={(v) => set({ severity: v as Severity })} options={(["sev1", "sev2", "sev3", "sev4"] as Severity[]).map((s) => ({ id: s, label: s.toUpperCase() }))} />
          </Field>
          <Field label="When it fires" hint={ACTION[d.action].hint}>
            <Select value={d.action} onChange={(e) => set({ action: e.target.value as Monitor["action"] })}>
              {Object.entries(ACTION).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
            </Select>
          </Field>
        </div>
        <Field label="Servers">
          <div className="flex flex-wrap items-center gap-3">
            {(["production", "staging", "development"] as Env[]).map((e) => (
              <label key={e} className="flex items-center gap-1.5 text-[13px]">
                <input type="checkbox" className="size-4 accent-[var(--arc)]" checked={envs.includes(e)}
                  onChange={(ev) => set({ scope: { ...d.scope, environments: ev.target.checked ? [...envs, e] : envs.filter((x) => x !== e) } })} />
                {titleCase(e)}
              </label>
            ))}
            <Select aria-label="Role" value={d.scope.role ?? ""} onChange={(e) => set({ scope: { ...d.scope, role: e.target.value || undefined } })} className="h-8 w-40">
              <option value="">Primaries and standbys</option><option value="primary">Primaries only</option><option value="replica">Standbys only</option>
            </Select>
            {!envs.length && <span className="text-[12px] text-ink-3">No environment ticked: all of them</span>}
          </div>
        </Field>
        <Field label="Description"><Input value={d.description} onChange={(e) => set({ description: e.target.value })} /></Field>
        <Field label="Runbook" hint="Markdown. rAIden reads it when it investigates an incident from this monitor."><Textarea rows={4} value={d.runbook} onChange={(e) => set({ runbook: e.target.value })} className="font-mono text-[12.5px]" /></Field>
        {save.error && <ErrorNote error={save.error} />}
      </div>
    </Drawer>
  );
}

export function MonitorsPage() {
  const monitors = useMonitors();
  const servers = useServers();
  const defOf = useMetricDefs();
  const me = useMe();
  const qc = useQueryClient();
  const toast = useToast();
  const [q, setQ] = useState("");
  const [cat, setCat] = useState("");
  const [firing, setFiring] = useState(false);
  const [editing, setEditing] = useState<Draft | null>(null);
  const isAdmin = me.data?.role === "admin";
  const byId = new Map((servers.data ?? []).map((s) => [s.id, s]));
  const patch = useMutation({
    mutationFn: ({ id, body }: { id: number; body: Record<string, unknown> }) => api.patch(`/monitors/${id}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["monitors"] }),
    onError: (e) => toast((e as Error).message, "crit"),
  });
  const remove = useMutation({
    mutationFn: (id: number) => api.del(`/monitors/${id}`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["monitors"] }); toast("Monitor deleted."); },
    onError: (e) => toast((e as Error).message, "crit"),
  });
  const list = monitors.data ?? [];
  const cats = [...new Set(list.map((m) => m.category))].sort();
  const shown = list.filter((m) => (!cat || m.category === cat) && (!firing || m.firing.length) && (!q || `${m.name} ${m.description} ${m.metric}`.toLowerCase().includes(q.toLowerCase())));
  const firingCount = list.filter((m) => m.firing.length).length;

  return (
    <div>
      <PageHeader title="Monitors" sub="Alert rules on the metrics of every server, checked at each collection. A firing monitor opens an incident for rAIden, sends a notification, or leaves a suggestion."
        actions={isAdmin && <Button variant="primary" onClick={() => setEditing({ ...blank })}><Plus className="size-4" /> New monitor</Button>} />
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="relative">
          <Search className="pointer-events-none absolute top-2.5 left-2.5 size-4 text-ink-3" />
          <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search monitors" className="w-60 pl-8" />
        </div>
        <Select aria-label="Category" value={cat} onChange={(e) => setCat(e.target.value)} className="w-48">
          <option value="">All categories</option>
          {cats.map((c) => <option key={c} value={c}>{categoryLabel(c)}</option>)}
        </Select>
        <label className="ml-1 flex items-center gap-2 text-[13px] font-semibold text-ink-2"><Switch size="sm" checked={firing} onChange={setFiring} /> Firing now ({firingCount})</label>
      </div>
      <Card className="overflow-hidden">
        {monitors.isLoading ? <Loading /> : shown.length === 0 ? <EmptyState icon={<BellRing className="size-7" />} title="No monitor matches" /> : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[980px] text-left text-[13px]">
              <thead className="bg-surface-2 text-[11.5px] text-ink-3">
                <tr>{["", "Monitor", "Rule", "Severity", "When it fires", "Firing on", ""].map((h, i) => <th key={i} className="px-3 py-2 font-semibold">{h}</th>)}</tr>
              </thead>
              <tbody>
                {shown.map((m) => (
                  <tr key={m.id} className={cn("border-t border-line align-top hover:bg-surface-2", !m.enabled && "opacity-60")}>
                    <td className="py-2.5 pr-1 pl-3"><Switch size="sm" checked={m.enabled} disabled={!isAdmin} onChange={(v) => patch.mutate({ id: m.id, body: { enabled: v } })} label={`${m.name} enabled`} /></td>
                    <td className="max-w-[300px] px-3 py-2">
                      <button className="text-left font-semibold hover:underline" onClick={() => setEditing({ ...m })}>{m.name}</button>
                      <div className="flex flex-wrap items-center gap-1.5 text-[12px] text-ink-3">
                        <span>{categoryLabel(m.category)}</span>
                        {m.builtin ? <Badge>Built-in</Badge> : <Badge tone="arc">Custom</Badge>}
                        {m.scope.environments?.length ? <span>· {m.scope.environments.map(titleCase).join(", ")}</span> : null}
                        {m.scope.role && <span>· {m.scope.role === "replica" ? "standbys" : "primaries"}</span>}
                      </div>
                    </td>
                    <td className="px-3 py-2 text-ink-2">{ruleText(m, defOf)}</td>
                    <td className="px-3 py-2"><SeverityBadge severity={m.severity} /></td>
                    <td className="px-3 py-2 text-ink-2">{ACTION[m.action].label}</td>
                    <td className="px-3 py-2">
                      {m.firing.length ? (
                        <div className="flex flex-col gap-0.5">
                          {m.firing.map((sid) => <ServerTag key={sid} name={byId.get(sid)?.name ?? sid} slot={byId.get(sid)?.color_slot} className="text-[12.5px] text-crit" />)}
                        </div>
                      ) : <span className="text-[12px] text-ink-3">—</span>}
                    </td>
                    <td className="px-3 py-2 text-right whitespace-nowrap">
                      {isAdmin && <Button size="sm" variant="ghost" onClick={() => setEditing({ ...m })}>Edit</Button>}
                      {isAdmin && !m.builtin && <Button size="sm" variant="ghost" onClick={() => remove.mutate(m.id)} aria-label="Delete"><Trash2 className="size-3.5" /></Button>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {editing && <Editor key={editing.id ?? "new"} draft={editing} onClose={() => setEditing(null)} />}
    </div>
  );
}
