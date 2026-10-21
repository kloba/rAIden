import { useMutation } from "@tanstack/react-query";
import { Check, Plus, PlugZap, X } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { ScoreRing } from "../components/assessment";
import { PLATFORM_CARDS } from "../components/serverform";
import { Badge, Button, Card, cn, EmptyState, Loading, PageHeader, ServerTag, Time, useToast } from "../components/ui";
import { api } from "../lib/api";
import { duration } from "../lib/format";
import { useEstate, useMe } from "../lib/hooks";

function TestButton({ id }: { id: string }) {
  const [res, setRes] = useState<{ ok: boolean; message: string } | null>(null);
  const toast = useToast();
  const test = useMutation({
    mutationFn: () => api.post<{ ok: boolean; message: string }>(`/servers/${id}/test`),
    onSuccess: (r) => { setRes(r); toast(r.message, r.ok ? "ok" : "crit"); },
    onError: (e) => toast((e as Error).message, "crit"),
  });
  return (
    <Button size="sm" variant="ghost" onClick={() => test.mutate()} loading={test.isPending} title={res?.message}>
      {res ? (res.ok ? <Check className="size-3.5 text-ok" /> : <X className="size-3.5 text-crit" />) : <PlugZap className="size-3.5" />} Test
    </Button>
  );
}

export function DatabasesPage() {
  const estate = useEstate();
  const me = useMe();
  const isAdmin = me.data?.role === "admin";
  const rows = estate.data ?? [];
  return (
    <div>
      <PageHeader title="Databases"
        sub="Connections, data sources and onboarding. rAIden needs a login with pg_monitor; everything it changes goes through the autonomy policy or your approval."
        actions={isAdmin && <Link to="/databases/new" className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-ink px-3.5 text-[13.5px] font-semibold text-surface hover:opacity-90"><Plus className="size-4" /> Add database</Link>} />
      <Card className="overflow-hidden">
        {estate.isLoading ? <Loading /> : rows.length === 0 ? (
          <EmptyState title="No databases yet" action={isAdmin && <Link to="/databases/new" className="inline-flex h-9 items-center rounded-lg bg-ink px-3.5 text-[13.5px] font-semibold text-surface">Add the first database</Link>}>
            On-prem, virtual machines, Kubernetes, Azure, AWS or Google Cloud: any PostgreSQL 13 or newer.
          </EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1100px] text-left text-[13px]">
              <thead className="bg-surface-2 text-[11.5px] text-ink-3">
                <tr>{["Server", "Connection", "Platform", "Data sources", "Collection", "Assessment", ""].map((h, i) => <th key={i} className="px-3 py-2 font-semibold">{h}</th>)}</tr>
              </thead>
              <tbody>
                {rows.map((s) => (
                  <tr key={s.id} className="border-t border-line align-top hover:bg-surface-2">
                    <td className="px-3 py-2.5">
                      <Link to={`/servers/${s.id}`} className="hover:underline"><ServerTag name={s.name} slot={s.color_slot} /></Link>
                      <div className="mt-0.5 pl-3.5 text-[12px] whitespace-nowrap text-ink-3">{s.environment}{s.owner && ` · ${s.owner}`}</div>
                    </td>
                    <td className="px-3 py-2.5">
                      <div className="mono max-w-[340px] truncate text-[12px]" title={`${s.username}@${s.host}:${s.port}/${s.dbname}`}>{s.username}@{s.host}:{s.port}/{s.dbname}</div>
                      <div className="mt-0.5 text-[12px] text-ink-3">TLS {s.sslmode}{s.databases.length ? ` · ${s.databases.join(", ")}` : " · all databases"}</div>
                    </td>
                    <td className="px-3 py-2.5">
                      <div className="whitespace-nowrap">{PLATFORM_CARDS.find((c) => c.id === s.platform)?.title ?? s.platform_label}</div>
                      <div className="text-[12px] text-ink-3">{s.version ? s.version.split(" ").slice(0, 2).join(" ") : "version unknown"}</div>
                    </td>
                    <td className="px-3 py-2.5">
                      <div className="flex flex-wrap gap-1">
                        <Badge>SQL</Badge>
                        {s.azure_resource_id && <Badge tone="info">Azure Monitor</Badge>}
                        {s.host_agent ? <Badge tone="arc">Host agent</Badge> : s.has_agent_token ? <Badge tone="warn">Agent: no heartbeat</Badge> : null}
                        {s.simulated && <Badge className="border-dashed">Simulated</Badge>}
                      </div>
                      {Object.keys(s.capabilities ?? {}).length > 0 && (() => {
                        const caps = s.capabilities;
                        if (caps.superuser) return <div className="mt-1 text-[11.5px] font-semibold text-warn">Connected as a superuser: use a pg_monitor login</div>;
                        const missing = Object.entries(caps).filter(([k, v]) => !v && k !== "superuser").map(([k]) => k.replace(/_/g, " "));
                        return <div className="mt-1 text-[11.5px] text-ink-3">{missing.length ? `Not granted: ${missing.join(", ")}` : "All grants in place"}</div>;
                      })()}
                    </td>
                    <td className="px-3 py-2.5 text-[12.5px] whitespace-nowrap">
                      {s.enabled ? <>every {duration(s.collect_interval_s)}<div className="text-ink-3">last <Time iso={s.last_seen_at} /></div></> : <Badge tone="warn">Paused</Badge>}
                      {s.last_error && <div className="mt-0.5 line-clamp-2 max-w-[220px] text-[12px] text-crit">{s.last_error}</div>}
                    </td>
                    <td className="px-3 py-2.5">
                      {(() => {
                        const issues = s.chips.issues.filter((c) => c.source === "assessment").length;
                        const sugg = s.chips.suggestions.filter((c) => c.source === "assessment").length;
                        if (!s.assessment) return <Link to={`/servers/${s.id}?tab=assessment`} className="text-[12.5px] font-semibold text-arc-ink hover:underline">Not assessed yet</Link>;
                        return (
                          <Link to={`/servers/${s.id}?tab=assessment`} className="group flex items-center gap-2.5 text-[12.5px] whitespace-nowrap">
                            <ScoreRing score={s.assessment.score} size={36} />
                            <span>
                              <span className={cn("font-semibold group-hover:underline", issues ? "text-crit" : sugg ? "text-lamp-ink" : "text-ok")}>
                                {issues ? `${issues} issue${issues > 1 ? "s" : ""}` : sugg ? `${sugg} suggestion${sugg > 1 ? "s" : ""}` : "No open findings"}
                              </span>
                              <span className="block text-ink-3">{issues > 0 && sugg > 0 ? `${sugg} suggestion${sugg > 1 ? "s" : ""} · ` : ""}<Time iso={s.assessment.finished_at} /></span>
                            </span>
                          </Link>
                        );
                      })()}
                    </td>
                    <td className="px-3 py-2.5 text-right whitespace-nowrap">
                      {me.data?.role !== "viewer" && <TestButton id={s.id} />}
                      <Link to={`/servers/${s.id}?tab=settings`} className="ml-1 inline-flex h-7 items-center rounded-lg px-2.5 text-[12.5px] font-semibold text-ink-2 hover:bg-surface-3 hover:text-ink">Configure</Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
