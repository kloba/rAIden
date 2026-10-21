import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { Button, Field, Modal, RiskBadge, Textarea, useToast } from "../../components/ui";
import { api } from "../../lib/api";
import { useCatalog } from "../../lib/hooks";

export interface PendingAction {
  action: string;
  args: Record<string, unknown>;
  title: string;
  detail?: ReactNode;
}

/** Run a typed action on a server by hand. The person who confirms is the approval; the reason lands in the audit log. */
export function useActionDialog(serverId: string, serverName: string): [(a: PendingAction) => void, ReactNode] {
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [reason, setReason] = useState("");
  const catalog = useCatalog();
  const qc = useQueryClient();
  const toast = useToast();
  const def = pending ? catalog.data?.actions.find((a) => a.id === pending.action) : undefined;
  const run = useMutation({
    mutationFn: (a: PendingAction) => api.post<{ message: string }>(`/servers/${serverId}/actions`, { action: a.action, args: a.args, reason }),
    onSuccess: (res) => {
      toast(`${def?.title ?? "Done"} on ${serverName}: ${res.message}`);
      setPending(null);
      setReason("");
      for (const k of ["live", "server", "metrics", "estate"]) qc.invalidateQueries({ queryKey: [k] });
    },
    onError: (e) => toast((e as Error).message, "crit"),
  });
  const dialog = (
    <Modal open={!!pending} onClose={() => setPending(null)} title={pending?.title ?? ""}
      footer={<><Button variant="ghost" onClick={() => setPending(null)}>Cancel</Button>
        <Button variant={def?.risk === "high" ? "danger" : "primary"} loading={run.isPending} onClick={() => pending && run.mutate(pending)}>{def?.title ?? "Run"}</Button></>}>
      {pending && (
        <div className="space-y-3 text-[13.5px]">
          <div className="rounded-lg border border-line bg-surface-2 px-3 py-2">
            <div className="flex flex-wrap items-center gap-2"><span className="font-semibold">{def?.title ?? pending.action}</span>{def && <RiskBadge risk={def.risk} />}{def && !def.reversible && <span className="text-[12px] text-ink-3">cannot be undone</span>}</div>
            <code className="mono mt-1 block text-[12px] text-ink-2">{pending.action}({Object.entries(pending.args).map(([k, v]) => `${k}=${v}`).join(", ")})</code>
            {def?.description && <div className="mt-1 text-[12.5px] text-ink-3">{def.description}</div>}
          </div>
          {pending.detail}
          <Field label="Reason" hint="Recorded in the audit log with your name">
            <Textarea rows={2} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Why this is safe now" />
          </Field>
          <p className="text-[12px] text-ink-3">Runs on {serverName} right away. Doing it yourself bypasses the autonomy gates; you are the approval.</p>
        </div>
      )}
    </Modal>
  );
  return [setPending, dialog];
}
