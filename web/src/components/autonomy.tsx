import { useMutation, useQueryClient } from "@tanstack/react-query";
import { KeyRound } from "lucide-react";
import { useState } from "react";
import { api } from "../lib/api";
import { usePolicy } from "../lib/hooks";
import type { Mode, Server, Stage } from "../lib/types";
import { CopyBlock } from "./assessment";
import { Breaker } from "./circuit";
import { Button, cn, Segmented, Time, useToast } from "./ui";

const GATED: Stage[] = ["triage", "diagnose", "remediate", "verify", "harden", "resolved"];

/** Per-server autonomy: every gate either follows the environment's policy or is pinned to autopilot / approval. */
export function ServerAutonomy({ server, canEdit }: { server: Server; canEdit: boolean }) {
  const policy = usePolicy();
  const qc = useQueryClient();
  const toast = useToast();
  const [local, setLocal] = useState<Partial<Record<Stage, Mode>>>(server.autonomy);
  const save = useMutation({
    mutationFn: (body: Record<string, Mode | null>) => api.put<Server>(`/servers/${server.id}/autonomy`, body),
    onSuccess: (s) => {
      setLocal(s.autonomy);
      for (const k of ["server", "servers", "estate", "autonomy"]) qc.invalidateQueries({ queryKey: [k] });
    },
    onError: (e) => toast((e as Error).message, "crit"),
  });
  const envPolicy = policy.data?.policy.environments[server.environment] ?? {};
  const info = new Map((policy.data?.stages ?? []).map((s) => [s.id, s]));
  return (
    <div className="divide-y divide-line">
      {GATED.map((st) => {
        const inherited = (envPolicy[st] as Mode | undefined) ?? "human";
        const pinned = local[st];
        const effective = pinned ?? inherited;
        return (
          <div key={st} className="flex flex-wrap items-center gap-3 py-2.5">
            <Breaker mode={effective} label={`${info.get(st)?.title}: ${effective === "auto" ? "autopilot" : "a person approves"}`} size={26} />
            <div className="min-w-[180px] flex-1">
              <div className="text-[13.5px] font-semibold">{info.get(st)?.title ?? st}</div>
              <div className="text-[12px] text-ink-3">{effective === "auto" ? info.get(st)?.auto : info.get(st)?.human}</div>
            </div>
            <Segmented size="sm" value={pinned ?? "inherit"}
              onChange={(v) => {
                if (!canEdit) return;
                const mode = v === "inherit" ? null : (v as Mode);
                setLocal((x) => { const n = { ...x }; if (mode) n[st] = mode; else delete n[st]; return n; });
                save.mutate({ [st]: mode });
              }}
              options={[
                { id: "inherit", label: <span>Like {server.environment} <span className={cn("font-normal", inherited === "auto" ? "text-arc-ink" : "text-lamp-ink")}>({inherited === "auto" ? "autopilot" : "approval"})</span></span> },
                { id: "auto", label: "Autopilot" },
                { id: "human", label: "Approval" },
              ]} />
          </div>
        );
      })}
    </div>
  );
}

/** The host agent's token: shown once, as the lines to put in the agent's environment file. */
export function AgentToken({ server, canEdit }: { server: Server; canEdit: boolean }) {
  const [env, setEnv] = useState<string | null>(null);
  const qc = useQueryClient();
  const toast = useToast();
  const create = useMutation({
    mutationFn: () => api.post<{ token: string; env: string }>(`/servers/${server.id}/agent-token`),
    onSuccess: (r) => {
      setEnv(r.env);
      qc.invalidateQueries({ queryKey: ["server", server.id] });
    },
    onError: (e) => toast((e as Error).message, "crit"),
  });
  return (
    <div>
      <p className="text-[13px] text-ink-2">
        The host agent runs next to PostgreSQL. It reads the server log and the operating system, so it catches what SQL cannot see: crashes, corruption,
        dropped tables, a full disk. It sends CPU, memory and disk metrics here and puts the incidents it handles on the board.
      </p>
      <div className="mt-2 text-[12.5px] text-ink-3">
        {server.agent_last_seen_at ? <>Last heartbeat <Time iso={server.agent_last_seen_at} />.</> : server.has_agent_token ? "A token exists; no heartbeat yet." : "Not connected."}
      </div>
      {env ? (
        <div className="mt-3">
          <div className="text-[13px] font-semibold">Add these lines to /etc/raiden/raiden.env on the database host, then run <code className="mono">systemctl restart raiden</code>:</div>
          <CopyBlock text={env} />
          <div className="mt-1 text-[12px] text-lamp-ink">The token is shown only now. Creating a new one disconnects the old agent.</div>
        </div>
      ) : canEdit && (
        <Button className="mt-3" size="sm" onClick={() => create.mutate()} loading={create.isPending}>
          <KeyRound className="size-3.5" /> {server.has_agent_token ? "Replace the agent token" : "Create an agent token"}
        </Button>
      )}
    </div>
  );
}
