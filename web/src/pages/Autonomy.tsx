import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link } from "react-router";
import { Breaker } from "../components/circuit";
import { Badge, Button, Card, CardHeader, cn, Input, Loading, Modal, PageHeader, RiskBadge, ServerTag, Switch, useToast } from "../components/ui";
import { api } from "../lib/api";
import { duration, titleCase } from "../lib/format";
import { useCatalog, useMe, usePolicy, useServers } from "../lib/hooks";
import type { Env, Mode, Stage } from "../lib/types";

const GATED: Stage[] = ["triage", "diagnose", "remediate", "verify", "harden", "resolved"];
const ENVS: Env[] = ["production", "staging", "development"];

export function AutonomyPage() {
  const policy = usePolicy();
  const catalog = useCatalog();
  const servers = useServers();
  const me = useMe();
  const qc = useQueryClient();
  const toast = useToast();
  const isAdmin = me.data?.role === "admin";
  const [confirm, setConfirm] = useState<{ env: Env; stage: Stage } | null>(null);
  const [verify, setVerify] = useState("");
  const [maxActions, setMaxActions] = useState("");
  const [learnAfter, setLearnAfter] = useState("");
  const p = policy.data?.policy;
  useEffect(() => {
    if (p) {
      setVerify(String(p.verify_window_s));
      setMaxActions(String(p.max_auto_actions));
      setLearnAfter(String(p.learn_after));
    }
  }, [p?.verify_window_s, p?.max_auto_actions, p?.learn_after]); // eslint-disable-line react-hooks/exhaustive-deps
  const put = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.put("/autonomy", body),
    onSuccess: () => { for (const k of ["autonomy", "estate", "incident", "knowledge"]) qc.invalidateQueries({ queryKey: [k] }); toast("Autonomy policy saved. It applies from the next stage of every incident."); },
    onError: (e) => toast((e as Error).message, "crit"),
  });
  if (policy.isLoading || !p) return <Loading />;
  const info = new Map(policy.data!.stages.map((s) => [s.id, s]));
  const flip = (env: Env, stage: Stage) => {
    const next: Mode = p.environments[env][stage] === "auto" ? "human" : "auto";
    if (next === "auto" && env === "production") setConfirm({ env, stage });
    else put.mutate({ environments: { [env]: { [stage]: next } } });
  };
  const highRisk = (catalog.data?.actions ?? []).filter((a) => a.risk === "high");
  const overrides = policy.data!.overrides;
  const slots = new Map((servers.data ?? []).map((s) => [s.id, s.color_slot]));

  return (
    <div>
      <PageHeader title="Autonomy"
        sub="Where rAIden carries on by itself and where a person approves first. Each switch is a gate in front of a stage; a server can pin its own gates." />

      <Card className="mb-4 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[920px] border-collapse">
            <thead>
              <tr className="border-b border-line">
                <th className="w-[170px] px-4 py-3 text-left"><span className="plate">Environment</span></th>
                {GATED.map((st) => (
                  <th key={st} className="px-2 py-3 text-left align-bottom">
                    <div className="display text-[15px] tracking-wide text-ink uppercase">{info.get(st)?.title}</div>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {ENVS.map((env) => {
                const gates = GATED.filter((st) => p.environments[env][st] === "human").length;
                return (
                  <tr key={env} className="border-b border-line last:border-0">
                    <td className="px-4 py-3 align-top">
                      <div className="text-[14px] font-semibold">{titleCase(env)}</div>
                      <div className={cn("text-[12px] font-semibold", gates ? "text-lamp-ink" : "text-arc-ink")}>{gates ? `${gates} gate${gates > 1 ? "s" : ""} for a person` : "Full autopilot"}</div>
                    </td>
                    {GATED.map((st) => {
                      const mode = p.environments[env][st] as Mode;
                      return (
                        <td key={st} className={cn("px-2 py-3 align-top", mode === "human" && "bg-lamp-soft/35")}>
                          <div className="flex items-center gap-1.5">
                            <Breaker mode={mode} onToggle={isAdmin ? () => flip(env, st) : undefined} disabled={!isAdmin || put.isPending}
                              label={`${titleCase(env)} · ${info.get(st)?.title}: ${mode === "auto" ? "autopilot" : "a person approves"}${isAdmin ? " (click to switch)" : ""}`} />
                            <span className={cn("text-[11.5px] font-bold tracking-wide uppercase", mode === "human" ? "text-lamp-ink" : "text-arc-ink")}>{mode === "human" ? "Approval" : "Autopilot"}</span>
                          </div>
                          <div className="mt-1 max-w-[190px] text-[11.5px] leading-snug text-ink-3">{mode === "auto" ? info.get(st)?.auto : info.get(st)?.human}</div>
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader title="Guardrails that stay on" sub="They apply on top of the gates, on every environment." />
          <div className="divide-y divide-line px-4 pb-2">
            <div className="flex items-start justify-between gap-4 py-3">
              <div>
                <div className="text-[13.5px] font-semibold">High-risk actions always wait for a person</div>
                <div className="mt-0.5 text-[12.5px] text-ink-3">Even on full autopilot:</div>
                <div className="mt-1.5 flex flex-wrap gap-1.5">{highRisk.map((a) => <span key={a.id} className="inline-flex items-center gap-1.5 text-[12px]"><RiskBadge risk="high" />{a.title}</span>)}</div>
              </div>
              <Switch checked={p.high_risk_always_human} disabled={!isAdmin} onChange={(v) => put.mutate({ high_risk_always_human: v })} label="High-risk actions always need a person" />
            </div>
            <div className="flex items-end justify-between gap-4 py-3">
              <div>
                <div className="text-[13.5px] font-semibold">Longer plans need a person</div>
                <div className="mt-0.5 text-[12.5px] text-ink-3">A plan with more actions than this waits for approval, whatever the gate says.</div>
              </div>
              <div className="flex items-center gap-2">
                <Input type="number" aria-label="Most actions in a plan rAIden may run without a person" min={1} max={50} value={maxActions} disabled={!isAdmin} onChange={(e) => setMaxActions(e.target.value)} className="w-20" />
                {isAdmin && Number(maxActions) !== p.max_auto_actions && <Button size="sm" onClick={() => put.mutate({ max_auto_actions: Number(maxActions) })}>Save</Button>}
              </div>
            </div>
            <div className="flex items-end justify-between gap-4 py-3">
              <div>
                <div className="text-[13.5px] font-semibold">Stop asking what people always approve</div>
                <div className="mt-0.5 text-[12.5px] text-ink-3">
                  After this many approvals in a row of the same action for the same kind of incident, rAIden offers not to ask again. An admin accepts it on
                  the <Link to="/knowledge" className="font-semibold text-arc-ink hover:underline">Knowledge</Link> page; the next rejection takes it back. Off: rAIden asks every time.
                </div>
              </div>
              <div className="flex items-center gap-2">
                <Input type="number" aria-label="Approvals in a row before rAIden offers not to ask again" min={2} max={20} value={learnAfter} disabled={!isAdmin || !p.learn_approvals}
                  onChange={(e) => setLearnAfter(e.target.value)} className="w-16" />
                {isAdmin && Number(learnAfter) !== p.learn_after && <Button size="sm" onClick={() => put.mutate({ learn_after: Number(learnAfter) })}>Save</Button>}
                <Switch checked={p.learn_approvals} disabled={!isAdmin} onChange={(v) => put.mutate({ learn_approvals: v })} label="Learn from approvals" />
              </div>
            </div>
            <div className="flex items-end justify-between gap-4 py-3">
              <div>
                <div className="text-[13.5px] font-semibold">Verification window</div>
                <div className="mt-0.5 text-[12.5px] text-ink-3">After a fix, rAIden watches the metrics this long ({duration(p.verify_window_s)}). If the problem comes back, the incident reopens.</div>
              </div>
              <div className="flex items-center gap-2">
                <Input type="number" aria-label="Verification window in seconds" min={15} max={3600} value={verify} disabled={!isAdmin} onChange={(e) => setVerify(e.target.value)} className="w-24" />
                <span className="text-[12.5px] text-ink-3">s</span>
                {isAdmin && Number(verify) !== p.verify_window_s && <Button size="sm" onClick={() => put.mutate({ verify_window_s: Number(verify) })}>Save</Button>}
              </div>
            </div>
            <div className="py-3">
              <div className="text-[13.5px] font-semibold">No AI endpoint or budget left</div>
              <div className="mt-0.5 text-[12.5px] text-ink-3">Every gate turns into an approval and incidents wait for a person. Monitoring and check routines keep running.</div>
            </div>
          </div>
        </Card>

        <Card>
          <CardHeader title="Servers with their own gates" sub="Set on the server's Settings tab. Everything else follows its environment." />
          {overrides.length === 0 ? <div className="px-4 pb-4 text-[13px] text-ink-3">No server overrides its environment.</div> : (
            <ul className="divide-y divide-line px-4 pb-2">
              {overrides.map((o) => (
                <li key={o.id} className="flex flex-wrap items-center gap-2 py-2.5">
                  <Link to={`/servers/${o.id}?tab=settings`} className="min-w-[140px] font-semibold hover:underline">
                    <ServerTag name={o.name} slot={slots.get(o.id)} />
                  </Link>
                  <span className="text-[12px] text-ink-3">{o.environment}</span>
                  <span className="flex flex-wrap gap-1">
                    {Object.entries(o.autonomy).map(([st, mode]) => <Badge key={st} tone={mode === "human" ? "lamp" : "arc"}>{info.get(st as Stage)?.title}: {mode === "human" ? "approval" : "autopilot"}</Badge>)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <Modal open={!!confirm} onClose={() => setConfirm(null)} title={`Autopilot for ${confirm ? info.get(confirm.stage)?.title : ""} on production?`}
        footer={<><Button variant="ghost" onClick={() => setConfirm(null)}>Keep approval</Button>
          <Button variant="arc" onClick={() => { if (confirm) put.mutate({ environments: { [confirm.env]: { [confirm.stage]: "auto" } } }); setConfirm(null); }}>Switch to autopilot</Button></>}>
        <p className="text-[13.5px] text-ink-2">{confirm && info.get(confirm.stage)?.auto} High-risk actions still wait for a person{p.high_risk_always_human ? "" : " only if that guardrail is on (it is off now)"}.</p>
      </Modal>
    </div>
  );
}

