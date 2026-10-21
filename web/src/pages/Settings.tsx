import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus, Send, Trash2, UserPlus } from "lucide-react";
import { useEffect, useState } from "react";
import {
  Avatar, Badge, Button, Card, CardHeader, Field, Input, KeyValue, Loading, Modal, PageHeader, Select, Switch, Tabs, Time, useToast,
} from "../components/ui";
import { api } from "../lib/api";
import { duration } from "../lib/format";
import { useMe, useSettings, useUsers } from "../lib/hooks";
import type { Role, SettingsPayload, User } from "../lib/types";

type Hook = SettingsPayload["notifications"]["webhooks"][number];
const EVENTS: { id: string; label: string }[] = [
  { id: "approval", label: "A decision is waiting" },
  { id: "incident_opened", label: "Incident opened" },
  { id: "escalated", label: "Escalated to on-call" },
  { id: "resolved", label: "Incident resolved" },
];
const ROLE_TEXT: Record<Role, string> = {
  admin: "Everything, including servers, AI endpoints, the autonomy policy and people",
  operator: "Decides approvals, runs actions and routines, investigates",
  viewer: "Reads everything, changes nothing",
};

function General({ s, isAdmin }: { s: SettingsPayload; isAdmin: boolean }) {
  const [g, setG] = useState(s.general);
  const qc = useQueryClient();
  const toast = useToast();
  const save = useMutation({
    mutationFn: () => api.put("/settings/general", g),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["settings"] }); toast("Settings saved."); },
    onError: (e) => toast((e as Error).message, "crit"),
  });
  return (
    <Card className="max-w-3xl">
      <CardHeader title="General" />
      <fieldset disabled={!isAdmin} className="grid gap-3 px-4 pb-4 sm:grid-cols-2">
        <Field label="Organization" hint="Shown on the overview and in reports"><Input value={g.organization ?? ""} onChange={(e) => setG({ ...g, organization: e.target.value })} /></Field>
        <Field label="Time zone" hint="For schedules and reports, e.g. Europe/Prague"><Input value={g.timezone ?? "UTC"} onChange={(e) => setG({ ...g, timezone: e.target.value })} /></Field>
        <Field label="Keep raw metrics (days)" hint="Full resolution, one sample per collection"><Input type="number" min={1} max={90} value={g.retention_raw_days} onChange={(e) => setG({ ...g, retention_raw_days: Number(e.target.value) })} /></Field>
        <Field label="Keep 15-minute rollups (days)" hint="For the 7- and 30-day views"><Input type="number" min={7} max={3650} value={g.retention_rollup_days} onChange={(e) => setG({ ...g, retention_rollup_days: Number(e.target.value) })} /></Field>
      </fieldset>
      {isAdmin && <div className="flex justify-end border-t border-line px-4 py-3"><Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}>Save changes</Button></div>}
    </Card>
  );
}

function Notifications({ s, isAdmin }: { s: SettingsPayload; isAdmin: boolean }) {
  const [hooks, setHooks] = useState<Hook[]>(s.notifications.webhooks ?? []);
  const qc = useQueryClient();
  const toast = useToast();
  const [results, setResults] = useState<{ name: string; ok: boolean; status: number | string }[] | null>(null);
  const save = useMutation({
    mutationFn: () => api.put("/settings/notifications", { webhooks: hooks }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["settings"] }); toast("Notifications saved."); },
    onError: (e) => toast((e as Error).message, "crit"),
  });
  const test = useMutation({
    mutationFn: () => api.post<{ results: { name: string; ok: boolean; status: number | string }[] }>("/settings/notifications/test"),
    onSuccess: (r) => setResults(r.results),
  });
  const set = (i: number, p: Partial<Hook>) => setHooks((h) => h.map((x, j) => (j === i ? { ...x, ...p } : x)));
  return (
    <Card className="max-w-4xl">
      <CardHeader title="Notifications" sub="Where rAIden tells people that a decision is waiting, an incident opened, or it handed over to on-call. Microsoft Teams and Slack incoming webhooks, or any HTTPS endpoint (JSON)." />
      <div className="space-y-3 px-4 pb-4">
        {hooks.length === 0 && <div className="rounded-lg border border-dashed border-line px-3 py-4 text-center text-[13px] text-ink-3">No webhooks. Decisions still show in the console and the header lamp.</div>}
        {hooks.map((h, i) => (
          <fieldset key={i} disabled={!isAdmin} className="rounded-xl border border-line p-3">
            <div className="grid gap-3 sm:grid-cols-[1fr_140px_auto]">
              <Field label="Name"><Input value={h.name} onChange={(e) => set(i, { name: e.target.value })} placeholder="DBA channel" /></Field>
              <Field label="Kind">
                <Select value={h.kind} onChange={(e) => set(i, { kind: e.target.value })}><option value="teams">Teams</option><option value="slack">Slack</option><option value="generic">Generic JSON</option></Select>
              </Field>
              <div className="flex items-end gap-2 pb-1.5">
                <Switch checked={h.enabled} onChange={(v) => set(i, { enabled: v })} label="Enabled" />
                <Button size="sm" variant="ghost" onClick={() => setHooks((x) => x.filter((_, j) => j !== i))} aria-label="Remove"><Trash2 className="size-3.5" /></Button>
              </div>
            </div>
            <Field label="URL" className="mt-3"><Input value={h.url} onChange={(e) => set(i, { url: e.target.value })} placeholder="https://…" className="font-mono text-[12.5px]" /></Field>
            <div className="mt-3 flex flex-wrap gap-3">
              {EVENTS.map((ev) => (
                <label key={ev.id} className="flex items-center gap-1.5 text-[13px]">
                  <input type="checkbox" className="size-4 accent-[var(--arc)]" checked={h.events.includes(ev.id)} onChange={(e) => set(i, { events: e.target.checked ? [...h.events, ev.id] : h.events.filter((x) => x !== ev.id) })} />
                  {ev.label}
                </label>
              ))}
            </div>
            {results?.find((r) => r.name === h.name) && (() => {
              const r = results.find((x) => x.name === h.name)!;
              return <div className={`mt-2 text-[12.5px] font-semibold ${r.ok ? "text-ok" : "text-crit"}`}>Test: {r.ok ? `delivered (${r.status})` : `failed (${r.status})`}</div>;
            })()}
          </fieldset>
        ))}
        {isAdmin && (
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" onClick={() => setHooks((h) => [...h, { name: "", url: "", kind: "teams", events: ["approval", "escalated", "incident_opened", "resolved"], enabled: true }])}><Plus className="size-3.5" /> Add webhook</Button>
            <div className="flex-1" />
            <Button size="sm" variant="ghost" onClick={() => test.mutate()} loading={test.isPending} disabled={!s.notifications.webhooks?.length}><Send className="size-3.5" /> Send a test to the saved webhooks</Button>
            <Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}>Save notifications</Button>
          </div>
        )}
      </div>
    </Card>
  );
}

function People({ me }: { me: User }) {
  const users = useUsers();
  const qc = useQueryClient();
  const toast = useToast();
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState({ email: "", name: "", role: "operator" as Role, password: "" });
  const patch = useMutation({
    mutationFn: ({ id, body }: { id: number; body: Record<string, unknown> }) => api.patch(`/users/${id}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["users"] }),
    onError: (e) => toast((e as Error).message, "crit"),
  });
  const create = useMutation({
    mutationFn: () => api.post("/users", form),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["users"] }); setAdding(false); setForm({ email: "", name: "", role: "operator", password: "" }); toast("Account created. Share the password through a safe channel."); },
    onError: (e) => toast((e as Error).message, "crit"),
  });
  const remove = useMutation({
    mutationFn: (id: number) => api.del(`/users/${id}`),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["users"] }); toast("Account deleted."); },
    onError: (e) => toast((e as Error).message, "crit"),
  });
  return (
    <Card className="max-w-5xl">
      <CardHeader title="People" sub="Who can use the console and what they may do." actions={<Button size="sm" variant="primary" onClick={() => setAdding(true)}><UserPlus className="size-3.5" /> Add a person</Button>} />
      {users.isLoading ? <Loading /> : (
        <table className="w-full text-left text-[13px]">
          <thead className="bg-surface-2 text-[11.5px] text-ink-3"><tr>{["Person", "Role", "Last sign-in", "Active", ""].map((h, i) => <th key={i} className="px-4 py-2 font-semibold">{h}</th>)}</tr></thead>
          <tbody>
            {(users.data ?? []).map((u) => (
              <tr key={u.id} className="border-t border-line">
                <td className="px-4 py-2.5">
                  <div className="flex items-center gap-2.5"><Avatar name={u.name || u.email} /><div><div className="font-semibold">{u.name || u.email}{u.id === me.id && <Badge className="ml-2">You</Badge>}</div><div className="text-[12px] text-ink-3">{u.email}</div></div></div>
                </td>
                <td className="px-4 py-2.5">
                  <Select value={u.role} disabled={u.id === me.id} onChange={(e) => patch.mutate({ id: u.id, body: { role: e.target.value } })} className="h-8 w-36" title={ROLE_TEXT[u.role]}>
                    <option value="admin">Admin</option><option value="operator">Operator</option><option value="viewer">Viewer</option>
                  </Select>
                </td>
                <td className="px-4 py-2.5 text-ink-2"><Time iso={u.last_login_at} /></td>
                <td className="px-4 py-2.5"><Switch size="sm" checked={!u.disabled} disabled={u.id === me.id} onChange={(v) => patch.mutate({ id: u.id, body: { disabled: !v } })} label="Active" /></td>
                <td className="px-4 py-2.5 text-right">{u.id !== me.id && <Button size="sm" variant="ghost" onClick={() => remove.mutate(u.id)} aria-label="Delete"><Trash2 className="size-3.5" /></Button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="border-t border-line px-4 py-3 text-[12px] text-ink-3">
        {(Object.keys(ROLE_TEXT) as Role[]).map((r) => <div key={r}><b className="text-ink-2 capitalize">{r}</b>: {ROLE_TEXT[r]}</div>)}
      </div>
      <Modal open={adding} onClose={() => setAdding(false)} title="Add a person"
        footer={<><Button variant="ghost" onClick={() => setAdding(false)}>Cancel</Button><Button variant="primary" loading={create.isPending} disabled={!form.email || form.password.length < 10} onClick={() => create.mutate()}>Create account</Button></>}>
        <div className="space-y-3">
          <Field label="Email"><Input type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} autoFocus /></Field>
          <Field label="Name"><Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field>
          <Field label="Role" hint={ROLE_TEXT[form.role]}>
            <Select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as Role })}><option value="admin">Admin</option><option value="operator">Operator</option><option value="viewer">Viewer</option></Select>
          </Field>
          <Field label="Initial password" hint="At least 10 characters, not only digits"><Input type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} autoComplete="new-password" /></Field>
        </div>
      </Modal>
    </Card>
  );
}

function System({ s }: { s: SettingsPayload }) {
  const x = s.system;
  return (
    <Card className="max-w-3xl">
      <CardHeader title="System" />
      <div className="px-4 pb-4">
        <KeyValue items={[
          ["Version", `rAIden console ${x.version}`],
          ["Mode", x.demo ? <Badge className="border-dashed">Demo: simulated servers and a scripted model</Badge> : "Production"],
          ["State store", x.database],
          ["Data directory", <span className="mono">{x.data_dir}</span>],
          ["Public URL", x.base_url || <span className="text-ink-3">not set (RAIDEN_BASE_URL)</span>],
          ["AI sessions at once", x.max_ai_sessions],
          ["Up for", duration(x.uptime_s)],
          ["Live connections", x.live_listeners],
          ["API", <a href="/api/docs" target="_blank" rel="noreferrer" className="font-semibold text-arc-ink hover:underline">OpenAPI reference</a>],
        ]} />
      </div>
    </Card>
  );
}

export function SettingsPage() {
  const settings = useSettings();
  const me = useMe();
  const isAdmin = me.data?.role === "admin";
  const [tab, setTab] = useState<"general" | "notifications" | "people" | "system">("general");
  useEffect(() => { if (!isAdmin && tab === "people") setTab("general"); }, [isAdmin, tab]);
  if (settings.isLoading || !settings.data || !me.data) return <Loading />;
  return (
    <div>
      <PageHeader title="Settings" sub={isAdmin ? undefined : "Only admins can change settings."} />
      <Tabs value={tab} onChange={setTab} className="mb-4"
        tabs={[{ id: "general", label: "General" }, { id: "notifications", label: "Notifications" }, ...(isAdmin ? [{ id: "people" as const, label: "People" }] : []), { id: "system", label: "System" }]} />
      {tab === "general" && <General s={settings.data} isAdmin={isAdmin} />}
      {tab === "notifications" && <Notifications s={settings.data} isAdmin={isAdmin} />}
      {tab === "people" && isAdmin && <People me={me.data} />}
      {tab === "system" && <System s={settings.data} />}
    </div>
  );
}
