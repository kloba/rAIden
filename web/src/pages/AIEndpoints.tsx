import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Check, CircleSlash, Cpu, KeyRound, Plus, ShieldCheck, Trash2, X, Zap } from "lucide-react";
import { useMemo, useState } from "react";
import { Meter, StackedBars } from "../components/charts";
import {
  Badge, Button, Card, CardHeader, cn, Drawer, EmptyState, ErrorNote, Field, Input, Loading, PageHeader, Segmented, Select, ServerTag, Stat, Switch, Time, useToast,
} from "../components/ui";
import { api } from "../lib/api";
import { compact, money, titleCase } from "../lib/format";
import { useEndpoints, useMe, useRouting, useServers, useUsage } from "../lib/hooks";
import type { Endpoint } from "../lib/types";

const PURPOSES = [
  { id: "incidents", label: "Incidents", hint: "Triage, diagnosis, fixes, reports" },
  { id: "chat", label: "Investigations", hint: "Ad-hoc questions in chat" },
  { id: "routines", label: "Routines", hint: "Scheduled AI reviews and summaries" },
  { id: "hardening", label: "Hardening", hint: "Protections after an incident" },
];
const PURPOSE_COLOR: Record<string, string> = { incidents: "var(--s1)", chat: "var(--s2)", routines: "var(--s3)", hardening: "var(--s4)" };
const PROVIDERS: Record<string, { label: string; url: string; auth: Endpoint["auth"][] }> = {
  azure_openai: { label: "Azure OpenAI / Microsoft Foundry", url: "https://<resource>.openai.azure.com/", auth: ["entra", "api_key"] },
  openai: { label: "OpenAI", url: "https://api.openai.com/v1", auth: ["api_key"] },
  anthropic: { label: "Anthropic", url: "https://api.anthropic.com", auth: ["api_key"] },
  openai_compatible: { label: "OpenAI-compatible (Ollama, vLLM, LM Studio, gateways)", url: "http://gpu-01.internal:11434/v1", auth: ["none", "api_key"] },
  demo: { label: "Demo (scripted)", url: "", auth: ["none"] },
};
const AUTH_LABEL: Record<Endpoint["auth"], string> = { entra: "Microsoft Entra ID (managed identity)", api_key: "API key", none: "No authentication" };

type Draft = Partial<Endpoint> & { api_key?: string };
const blank: Draft = { name: "", provider: "azure_openai", base_url: "", model: "", api_version: "2025-04-01-preview", wire_api: "auto", auth: "entra", reasoning_effort: "medium",
  max_output_tokens: 8000, price_in_per_m: 0, price_out_per_m: 0, price_cached_per_m: 0, daily_token_budget: null, monthly_cost_budget: null,
  purposes: ["incidents", "chat", "routines", "hardening"], priority: 10, enabled: true };

function Editor({ draft, onClose }: { draft: Draft; onClose: () => void }) {
  const [d, setD] = useState<Draft>(draft);
  const set = (p: Partial<Draft>) => setD((x) => ({ ...x, ...p }));
  const qc = useQueryClient();
  const toast = useToast();
  const isNew = !d.id;
  const save = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = {
        name: d.name, base_url: d.base_url, model: d.model, api_version: d.api_version, wire_api: d.wire_api, auth: d.auth, reasoning_effort: d.reasoning_effort,
        max_output_tokens: Number(d.max_output_tokens), price_in_per_m: Number(d.price_in_per_m), price_out_per_m: Number(d.price_out_per_m), price_cached_per_m: Number(d.price_cached_per_m),
        daily_token_budget: d.daily_token_budget ? Number(d.daily_token_budget) : null, monthly_cost_budget: d.monthly_cost_budget ? Number(d.monthly_cost_budget) : null,
        purposes: d.purposes, priority: Number(d.priority), enabled: d.enabled,
      };
      if (d.api_key) body.api_key = d.api_key;
      if (isNew) {
        body.provider = d.provider;
        return api.post("/ai/endpoints", body);
      }
      // send only what changed: a real URL, model or key is what turns a simulated endpoint into a live one
      const changed = Object.fromEntries(Object.entries(body).filter(([k, v]) => k === "api_key" || JSON.stringify(v) !== JSON.stringify((draft as Record<string, unknown>)[k] ?? null)));
      return api.patch(`/ai/endpoints/${d.id}`, changed);
    },
    onSuccess: () => { for (const k of ["endpoints", "routing"]) qc.invalidateQueries({ queryKey: [k] }); toast(isNew ? "Endpoint added. Test it before relying on it." : "Endpoint saved."); onClose(); },
  });
  const prov = PROVIDERS[d.provider ?? "azure_openai"]!;
  return (
    <Drawer open onClose={onClose} width={620} title={isNew ? "Add an AI endpoint" : d.name} sub="Keys are encrypted at rest and never shown again."
      footer={<><Button variant="ghost" onClick={onClose}>Cancel</Button><Button variant="primary" loading={save.isPending} disabled={!d.name || !d.model} onClick={() => save.mutate()}>{isNew ? "Add endpoint" : "Save endpoint"}</Button></>}>
      <div className="space-y-4">
        <Field label="Provider">
          <Select value={d.provider} disabled={!isNew} onChange={(e) => { const p = PROVIDERS[e.target.value]!; set({ provider: e.target.value as Endpoint["provider"], auth: p.auth[0], base_url: "" }); }}>
            {Object.entries(PROVIDERS).filter(([k]) => k !== "demo").map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
          </Select>
        </Field>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Name"><Input value={d.name} onChange={(e) => set({ name: e.target.value })} placeholder="Azure OpenAI · production" /></Field>
          <Field label={d.provider === "azure_openai" ? "Deployment" : "Model"}><Input value={d.model} onChange={(e) => set({ model: e.target.value })} placeholder={d.provider === "anthropic" ? "claude-sonnet-5" : d.provider === "openai_compatible" ? "qwen3-coder:30b" : "gpt-6.1"} /></Field>
        </div>
        <Field label="Endpoint URL"><Input value={d.base_url} onChange={(e) => set({ base_url: e.target.value })} placeholder={prov.url} className="font-mono text-[12.5px]" /></Field>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Authentication">
            <Select value={d.auth} onChange={(e) => set({ auth: e.target.value as Endpoint["auth"] })}>
              {prov.auth.map((a) => <option key={a} value={a}>{AUTH_LABEL[a]}</option>)}
            </Select>
          </Field>
          {d.auth === "api_key" && (
            <Field label="API key" hint={d.has_api_key ? "Stored. Leave empty to keep it." : undefined}>
              <Input type="password" value={d.api_key ?? ""} onChange={(e) => set({ api_key: e.target.value })} autoComplete="new-password" placeholder={d.has_api_key ? "••••••••" : ""} />
            </Field>
          )}
        </div>
        {(d.provider === "azure_openai" || d.provider === "openai") && (
          <div className="grid gap-3 sm:grid-cols-2">
            {d.provider === "azure_openai" && <Field label="API version"><Input value={d.api_version} onChange={(e) => set({ api_version: e.target.value })} /></Field>}
            <Field label="Wire API" hint="Auto picks the Responses API for the GPT-6 family">
              <Segmented size="sm" value={d.wire_api ?? "auto"} onChange={(v) => set({ wire_api: v })} options={[{ id: "auto", label: "Auto" }, { id: "chat", label: "Chat Completions" }, { id: "responses", label: "Responses" }]} />
            </Field>
          </div>
        )}
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Reasoning effort">
            <Select value={d.reasoning_effort} onChange={(e) => set({ reasoning_effort: e.target.value })}>
              {["none", "minimal", "low", "medium", "high", "xhigh"].map((x) => <option key={x} value={x}>{titleCase(x)}</option>)}
            </Select>
          </Field>
          <Field label="Max output tokens"><Input type="number" value={d.max_output_tokens} onChange={(e) => set({ max_output_tokens: Number(e.target.value) })} /></Field>
        </div>
        <Field label="Use it for" hint="rAIden picks the endpoint with the lowest priority number for each purpose and falls back to the next one.">
          <div className="grid gap-1.5 sm:grid-cols-2">
            {PURPOSES.map((p) => (
              <label key={p.id} className="flex items-start gap-2 rounded-lg border border-line px-2.5 py-2 text-[13px]">
                <input type="checkbox" className="mt-0.5 size-4 accent-[var(--arc)]" checked={d.purposes?.includes(p.id)} onChange={(e) => set({ purposes: e.target.checked ? [...(d.purposes ?? []), p.id] : (d.purposes ?? []).filter((x) => x !== p.id) })} />
                <span><span className="font-semibold">{p.label}</span><span className="block text-[12px] text-ink-3">{p.hint}</span></span>
              </label>
            ))}
          </div>
        </Field>
        <div className="grid gap-3 sm:grid-cols-4">
          <Field label="Priority" hint="1 = first"><Input type="number" min={1} max={100} value={d.priority} onChange={(e) => set({ priority: Number(e.target.value) })} /></Field>
          <Field label="$ per 1M in"><Input type="number" step="0.01" value={d.price_in_per_m} onChange={(e) => set({ price_in_per_m: Number(e.target.value) })} /></Field>
          <Field label="$ per 1M out"><Input type="number" step="0.01" value={d.price_out_per_m} onChange={(e) => set({ price_out_per_m: Number(e.target.value) })} /></Field>
          <Field label="$ per 1M cached"><Input type="number" step="0.001" value={d.price_cached_per_m} onChange={(e) => set({ price_cached_per_m: Number(e.target.value) })} /></Field>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Daily token budget" hint="Empty: no limit"><Input type="number" value={d.daily_token_budget ?? ""} onChange={(e) => set({ daily_token_budget: e.target.value ? Number(e.target.value) : null })} placeholder="5000000" /></Field>
          <Field label="Monthly cost budget ($)" hint="When spent, rAIden stops using it; every gate then waits for a person"><Input type="number" value={d.monthly_cost_budget ?? ""} onChange={(e) => set({ monthly_cost_budget: e.target.value ? Number(e.target.value) : null })} placeholder="300" /></Field>
        </div>
        {save.error && <ErrorNote error={save.error} />}
      </div>
    </Drawer>
  );
}

function EndpointCard({ e, isAdmin, onEdit }: { e: Endpoint; isAdmin: boolean; onEdit: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const test = useMutation({
    mutationFn: () => api.post<{ ok: boolean; message: string; latency_ms: number }>(`/ai/endpoints/${e.id}/test`),
    onSuccess: (r) => { qc.invalidateQueries({ queryKey: ["endpoints"] }); toast(r.ok ? `${e.name}: answered in ${r.latency_ms} ms` : `${e.name}: ${r.message}`, r.ok ? "ok" : "crit"); },
  });
  const patch = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.patch(`/ai/endpoints/${e.id}`, body),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["endpoints"] }); qc.invalidateQueries({ queryKey: ["routing"] }); },
  });
  const remove = useMutation({
    mutationFn: () => api.del(`/ai/endpoints/${e.id}`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["endpoints"] }); qc.invalidateQueries({ queryKey: ["routing"] }); toast(`${e.name} removed.`); },
  });
  return (
    <Card className={cn("flex flex-col", !e.enabled && "opacity-65")}>
      <div className="flex items-start gap-3 px-4 pt-4">
        <span className="mt-0.5 flex size-9 shrink-0 items-center justify-center rounded-lg bg-surface-3 text-ink-2"><Cpu className="size-4.5" /></span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[15px] font-semibold">{e.name}</span>
            {e.simulated && <Badge className="border-dashed" title="A scripted stand-in for the demo: edit it to point at a real endpoint">Simulated</Badge>}
          </div>
          <div className="text-[12.5px] text-ink-3">{PROVIDERS[e.provider]?.label ?? e.provider} · <span className="mono">{e.model}</span></div>
        </div>
        <Switch checked={e.enabled} disabled={!isAdmin} onChange={(v) => patch.mutate({ enabled: v })} label={`${e.name} enabled`} />
      </div>
      <div className="mt-3 space-y-2.5 px-4">
        <div className="mono truncate text-[12px] text-ink-2" title={e.base_url}>{e.base_url || "—"}</div>
        <div className="flex flex-wrap items-center gap-1.5 text-[12px]">
          <Badge>{e.auth === "entra" ? <><ShieldCheck className="size-3" /> Entra ID</> : e.auth === "api_key" ? <><KeyRound className="size-3" /> {e.has_api_key ? "API key stored" : e.simulated ? "API key" : "API key missing"}</> : "No auth"}</Badge>
          <Badge>Priority {e.priority}</Badge>
          {e.reasoning_effort !== "none" && <Badge>Reasoning {e.reasoning_effort}</Badge>}
          {e.purposes.map((p) => <span key={p} className="inline-flex items-center gap-1 rounded-md border border-line px-1.5 py-[1px] text-[11.5px] font-semibold text-ink-2"><span className="size-2 rounded-sm" style={{ background: PURPOSE_COLOR[p] }} />{PURPOSES.find((x) => x.id === p)?.label ?? p}</span>)}
        </div>
        <div className="grid grid-cols-2 gap-4 pt-1">
          <Meter value={e.tokens_today} max={e.daily_token_budget} label="Tokens today" sub={e.daily_token_budget ? `${compact(e.tokens_today)} of ${compact(e.daily_token_budget)}` : `${compact(e.tokens_today)} · no limit`} />
          <Meter value={e.cost_month} max={e.monthly_cost_budget} label="Spend this month" sub={e.monthly_cost_budget ? `${money(e.cost_month)} of ${money(e.monthly_cost_budget)}` : `${money(e.cost_month)} · no limit`} />
        </div>
        <div className="text-[12px] text-ink-3 num">
          {e.price_in_per_m || e.price_out_per_m ? `$${e.price_in_per_m} in · $${e.price_out_per_m} out · $${e.price_cached_per_m} cached per 1M tokens` : "No token price (self-hosted)"}
        </div>
      </div>
      <div className="mt-auto flex items-center gap-2 border-t border-line px-4 py-2.5 pt-2.5">
        <span className={cn("inline-flex min-w-0 flex-1 items-center gap-1.5 truncate text-[12px]", e.last_test_ok === false ? "text-crit" : "text-ink-3")}>
          {e.last_test_ok == null ? "Not tested yet" : e.last_test_ok ? <><Check className="size-3.5 text-ok" /> {e.last_latency_ms} ms · <Time iso={e.last_test_at} /></> : <><X className="size-3.5" /> {e.last_test_message}</>}
        </span>
        {isAdmin && !e.simulated && <Button size="sm" variant="ghost" onClick={() => remove.mutate()} aria-label="Remove"><Trash2 className="size-3.5" /></Button>}
        {isAdmin && <Button size="sm" variant="ghost" onClick={onEdit}>Edit</Button>}
        {isAdmin && <Button size="sm" onClick={() => test.mutate()} loading={test.isPending}><Zap className="size-3.5" /> Test</Button>}
      </div>
    </Card>
  );
}

export function AIEndpointsPage() {
  const [days, setDays] = useState(30);
  const endpoints = useEndpoints();
  const usage = useUsage(days);
  const routing = useRouting();
  const servers = useServers();
  const me = useMe();
  const isAdmin = me.data?.role === "admin";
  const [editing, setEditing] = useState<Draft | null>(null);
  const byServer = new Map((servers.data ?? []).map((s) => [s.id, s]));

  const daily = useMemo(() => {
    const rows = new Map<string, Record<string, any>>();
    const start = new Date();
    start.setUTCHours(0, 0, 0, 0);
    for (let i = days - 1; i >= 0; i--) {
      const d = new Date(start.getTime() - i * 86400_000).toISOString().slice(0, 10);
      rows.set(d, { day: d, incidents: 0, chat: 0, routines: 0, hardening: 0 });
    }
    for (const r of usage.data?.daily ?? []) {
      const row = rows.get(r.day);
      if (row) row[r.purpose] = (row[r.purpose] ?? 0) + r.tokens_in + r.tokens_out;
    }
    return [...rows.values()];
  }, [usage.data, days]);
  const byPurpose = useMemo(() => {
    const m: Record<string, { tokens: number; cost: number }> = {};
    for (const r of usage.data?.daily ?? []) {
      const x = (m[r.purpose] ??= { tokens: 0, cost: 0 });
      x.tokens += r.tokens_in + r.tokens_out;
      x.cost += r.cost_usd;
    }
    return m;
  }, [usage.data]);
  const cached = (usage.data?.daily ?? []).reduce((a, r) => a + r.tokens_cached, 0);
  const tokensIn = (usage.data?.daily ?? []).reduce((a, r) => a + r.tokens_in, 0);

  return (
    <div>
      <PageHeader title="AI endpoints"
        sub="The models rAIden thinks with, what each one is used for and what it costs. Azure OpenAI, OpenAI, Anthropic, or any OpenAI-compatible server, including models on your own GPUs."
        actions={<>
          <Segmented value={String(days)} onChange={(v) => setDays(Number(v))} options={[{ id: "7", label: "7 days" }, { id: "30", label: "30 days" }]} />
          {isAdmin && <Button variant="primary" onClick={() => setEditing({ ...blank })}><Plus className="size-4" /> Add endpoint</Button>}
        </>} />

      <div className="mb-4 grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
        <Card>
          <CardHeader title="Tokens per day" sub="By what rAIden was doing" />
          <div className="px-4 pb-4">
            {usage.isLoading ? <Loading /> : <StackedBars data={daily} keys={PURPOSES.map((p) => ({ key: p.id, label: p.label, color: PURPOSE_COLOR[p.id]! }))} height={300} />}
          </div>
        </Card>
        <Card>
          <div className="grid grid-cols-2 gap-4 px-4 py-4">
            <Stat label={`Tokens, ${days} days`} value={compact(usage.data?.totals.tokens ?? 0)} sub={`${(usage.data?.totals.calls ?? 0).toLocaleString("en-US")} model calls`} />
            <Stat label={`Spend, ${days} days`} value={money(usage.data?.totals.cost_usd ?? 0)} sub={tokensIn ? `${Math.round((100 * cached) / tokensIn)}% of input cached` : undefined} />
          </div>
          <div className="border-t border-line px-4 py-3">
            <div className="plate mb-2">By purpose</div>
            <ul className="space-y-1.5 text-[12.5px]">
              {PURPOSES.map((p) => (
                <li key={p.id} className="flex items-center gap-2">
                  <span className="size-2.5 rounded-sm" style={{ background: PURPOSE_COLOR[p.id] }} />
                  <span className="flex-1 text-ink-2">{p.label}</span>
                  <span className="font-semibold num">{compact(byPurpose[p.id]?.tokens ?? 0)}</span>
                  <span className="w-16 text-right text-ink-3 num">{money(byPurpose[p.id]?.cost ?? 0)}</span>
                </li>
              ))}
            </ul>
          </div>
          <div className="border-t border-line px-4 py-3">
            <div className="plate mb-2">By server</div>
            <ul className="space-y-1.5 text-[12.5px]">
              {(usage.data?.by_server ?? []).filter((x) => x.server_id).slice(0, 6).map((x) => (
                <li key={x.server_id} className="flex items-center gap-2">
                  <span className="min-w-0 flex-1"><ServerTag name={byServer.get(x.server_id)?.name ?? x.server_id} slot={byServer.get(x.server_id)?.color_slot} className="font-medium" /></span>
                  <span className="font-semibold num">{compact(x.tokens)}</span>
                  <span className="w-16 text-right text-ink-3 num">{money(x.cost_usd)}</span>
                </li>
              ))}
            </ul>
          </div>
        </Card>
      </div>

      <Card className="mb-4">
        <CardHeader title="Routing" sub="For each kind of work, the endpoints rAIden tries in order. An endpoint over its budget is skipped; with none left, every gate waits for a person." />
        <div className="grid gap-px border-t border-line bg-line sm:grid-cols-2 xl:grid-cols-4">
          {(routing.data ?? []).map((r) => (
            <div key={r.purpose} className="bg-surface px-4 py-3">
              <div className="mb-2 flex items-center gap-2 text-[13px] font-semibold"><span className="size-2.5 rounded-sm" style={{ background: PURPOSE_COLOR[r.purpose] }} />{PURPOSES.find((p) => p.id === r.purpose)?.label ?? r.purpose}</div>
              {r.endpoints.length === 0 ? (
                <div className="flex items-center gap-1.5 text-[12.5px] font-semibold text-lamp-ink"><CircleSlash className="size-3.5" /> No endpoint: a person does this work</div>
              ) : (
                <ol className="space-y-1">
                  {r.endpoints.map((e, i) => (
                    <li key={e.id} className="flex items-center gap-1.5 text-[12.5px]">
                      {i > 0 && <ArrowRight className="size-3 text-ink-3" />}
                      <span className={cn("truncate", i === 0 ? "font-semibold text-ink" : "text-ink-2")}>{e.name}</span>
                      {i === 0 && <Badge tone="arc">first</Badge>}
                    </li>
                  ))}
                </ol>
              )}
              {r.skipped.map((s, i) => <div key={i} className="mt-1 text-[11.5px] text-warn">Skipped: {s}</div>)}
            </div>
          ))}
        </div>
      </Card>

      {endpoints.isLoading ? <Loading /> : (endpoints.data ?? []).length === 0 ? (
        <Card><EmptyState icon={<Cpu className="size-7" />} title="No AI endpoint yet" action={isAdmin && <Button variant="primary" onClick={() => setEditing({ ...blank })}>Add an endpoint</Button>}>
          Without one, monitoring and check routines keep working and incidents still open, but every stage waits for a person.
        </EmptyState></Card>
      ) : (
        <div className="grid gap-4 lg:grid-cols-2 2xl:grid-cols-3">
          {endpoints.data!.map((e) => <EndpointCard key={e.id} e={e} isAdmin={isAdmin} onEdit={() => setEditing({ ...e })} />)}
        </div>
      )}
      {editing && <Editor key={editing.id ?? "new"} draft={editing} onClose={() => setEditing(null)} />}
    </div>
  );
}
