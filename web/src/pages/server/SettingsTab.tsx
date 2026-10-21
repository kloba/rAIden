import { useMutation, useQueryClient } from "@tanstack/react-query";
import { PlugZap, Save, Trash2 } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router";
import { AgentToken, ServerAutonomy } from "../../components/autonomy";
import { ConnectionFields, DescribeFields, draftBody, draftFrom, PLATFORM_CARDS, type ServerDraft } from "../../components/serverform";
import { Button, Card, CardHeader, Field, Input, Modal, Select, Switch, useToast } from "../../components/ui";
import { api } from "../../lib/api";
import type { Server, ServerDetail } from "../../lib/types";

export function SettingsTab({ server, isAdmin }: { server: ServerDetail; isAdmin: boolean }) {
  const [d, setD] = useState<ServerDraft>(() => draftFrom(server));
  const [dirty, setDirty] = useState(false);
  const set = (p: Partial<ServerDraft>) => { setD((x) => ({ ...x, ...p })); setDirty(true); };
  const [deleting, setDeleting] = useState(false);
  const [confirmId, setConfirmId] = useState("");
  const [test, setTest] = useState<{ ok: boolean; message: string } | null>(null);
  const qc = useQueryClient();
  const toast = useToast();
  const nav = useNavigate();
  const refresh = () => { for (const k of ["server", "servers", "estate", "overview"]) qc.invalidateQueries({ queryKey: [k] }); };
  const save = useMutation({
    mutationFn: () => api.patch<Server>(`/servers/${server.id}`, draftBody(d, false)),
    onSuccess: () => { setDirty(false); setD((x) => ({ ...x, password: "" })); refresh(); toast("Saved. The next collection uses the new settings."); },
    onError: (e) => toast((e as Error).message, "crit"),
  });
  const runTest = useMutation({
    mutationFn: () => api.post<{ ok: boolean; message: string }>(`/servers/${server.id}/test`),
    onSuccess: setTest,
    onError: (e) => setTest({ ok: false, message: (e as Error).message }),
  });
  const pause = useMutation({
    mutationFn: (enabled: boolean) => api.patch<Server>(`/servers/${server.id}`, { enabled }),
    onSuccess: (s) => { refresh(); toast(s.enabled ? "Monitoring resumed." : "Monitoring paused: no collection, no incidents."); },
  });
  const remove = useMutation({
    mutationFn: () => api.del(`/servers/${server.id}`),
    onSuccess: () => { refresh(); toast(`${server.name} removed.`); nav("/databases"); },
    onError: (e) => toast((e as Error).message, "crit"),
  });

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_440px]">
      <div className="space-y-4">
        <Card>
          <CardHeader title="Connection" sub={server.simulated ? "This is a simulated demo server: the connection settings are not used." : "How the console reaches the server. Read-only checks; nothing is changed."}
            actions={<Button size="sm" onClick={() => runTest.mutate()} loading={runTest.isPending}><PlugZap className="size-3.5" /> Test</Button>} />
          <div className="px-4 pb-4">
            {test && <div className={`mb-3 rounded-lg px-3 py-2 text-[12.5px] font-semibold ${test.ok ? "bg-ok-soft text-ok" : "bg-crit-soft text-crit"}`}>{test.message}</div>}
            <fieldset disabled={!isAdmin}>
              <div className="mb-3 grid gap-3 sm:grid-cols-2">
                <Field label="Platform">
                  <Select value={d.platform} onChange={(e) => set({ platform: e.target.value })}>
                    {PLATFORM_CARDS.map((p) => <option key={p.id} value={p.id}>{p.title} · {p.hint}</option>)}
                  </Select>
                </Field>
              </div>
              <ConnectionFields d={d} set={set} editing />
            </fieldset>
          </div>
        </Card>
        <Card>
          <CardHeader title="Details" />
          <fieldset disabled={!isAdmin} className="px-4 pb-4"><DescribeFields d={d} set={set} isNew={false} /></fieldset>
        </Card>
        {isAdmin && (
          <div className="sticky bottom-4 z-10 flex justify-end">
            <Button variant="primary" disabled={!dirty} loading={save.isPending} onClick={() => save.mutate()} className="shadow-pop"><Save className="size-4" /> Save changes</Button>
          </div>
        )}
      </div>

      <div className="space-y-4">
        <Card>
          <CardHeader title="Autonomy" sub="Pin a gate for this server, or let it follow its environment." />
          <div className="px-4 pb-2"><ServerAutonomy server={server} canEdit={isAdmin} /></div>
        </Card>
        <Card>
          <CardHeader title="Host agent" />
          <div className="px-4 pb-4"><AgentToken server={server} canEdit={isAdmin} /></div>
        </Card>
        {isAdmin && (
          <Card className="border-crit/30">
            <CardHeader title="Danger zone" />
            <div className="space-y-3 px-4 pb-4">
              <div className="flex items-center justify-between gap-3">
                <div><div className="text-[13.5px] font-semibold">Monitoring</div><div className="text-[12.5px] text-ink-3">Paused servers are not collected and open no incidents.</div></div>
                <Switch checked={server.enabled} onChange={(v) => pause.mutate(v)} label="Monitoring enabled" />
              </div>
              <div className="flex items-center justify-between gap-3 border-t border-line pt-3">
                <div><div className="text-[13.5px] font-semibold">Remove this server</div><div className="text-[12.5px] text-ink-3">Deletes its metrics, assessments and settings. Past incidents stay.</div></div>
                <Button variant="danger" size="sm" onClick={() => setDeleting(true)}><Trash2 className="size-3.5" /> Remove</Button>
              </div>
            </div>
          </Card>
        )}
      </div>

      <Modal open={deleting} onClose={() => setDeleting(false)} title={`Remove ${server.name}?`}
        footer={<><Button variant="ghost" onClick={() => setDeleting(false)}>Cancel</Button><Button variant="danger" disabled={confirmId !== server.id} loading={remove.isPending} onClick={() => remove.mutate()}>Remove server</Button></>}>
        <p className="mb-3 text-[13.5px] text-ink-2">Nothing changes on the PostgreSQL server itself. The console forgets it: metrics, assessments, suggestions and settings are deleted. Past incidents stay in the history.</p>
        <Field label={<>Type <code className="mono">{server.id}</code> to confirm</>}><Input value={confirmId} onChange={(e) => setConfirmId(e.target.value)} autoFocus /></Field>
      </Modal>
    </div>
  );
}
