import { CheckCircle2, Hand } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { ApprovalBox, STAGE_TITLE } from "../components/incident";
import { Badge, Card, EmptyState, Loading, PageHeader, Segmented, SeverityBadge, ServerTag, Time } from "../components/ui";
import { personName, titleCase } from "../lib/format";
import { useApprovals, useMe, useServers } from "../lib/hooks";

export function ApprovalsPage() {
  const [view, setView] = useState<"pending" | "all">("pending");
  const list = useApprovals(view);
  const me = useMe();
  const servers = useServers();
  const slots = new Map((servers.data ?? []).map((s) => [s.id, s.color_slot]));
  const canDecide = me.data?.role !== "viewer";
  const items = list.data ?? [];
  return (
    <div className="mx-auto max-w-[980px]">
      <PageHeader title="Needs you"
        sub="The decisions the autonomy policy keeps for people: gates before a stage, risky actions, and protections. rAIden waits for each one and carries on as soon as it is made."
        actions={<Segmented value={view} onChange={setView} options={[{ id: "pending", label: "Waiting" }, { id: "all", label: "History" }]} />} />
      {list.isLoading ? <Loading /> : items.length === 0 ? (
        <Card><EmptyState icon={<CheckCircle2 className="size-7 text-arc" />} title={view === "pending" ? "Nothing is waiting for you" : "No decisions yet"}>
          {view === "pending" ? "rAIden handles what the autonomy policy allows on its own. Decisions appear here, in the header lamp and in your notifications." : null}
        </EmptyState></Card>
      ) : (
        <div className="space-y-4">
          {items.map((a) => {
            const to = a.incident_id ? `/incidents/${a.incident_id}` : a.conversation_id ? `/chat/${a.conversation_id}` : null;
            return (
              <div key={a.id}>
                <div className="mb-1.5 flex flex-wrap items-center gap-2 px-1 text-[12.5px]">
                  {a.severity && <SeverityBadge severity={a.severity} />}
                  {to ? <Link to={to} className="mono font-semibold hover:underline">{a.incident_id ?? "Investigation"}</Link> : null}
                  {a.server_name && <ServerTag name={a.server_name} slot={slots.get(a.server_id ?? "")} />}
                  {a.incident_title && <Link to={to ?? "#"} className="min-w-0 truncate text-ink-2 hover:underline">{a.incident_title}</Link>}
                  <span className="ml-auto inline-flex items-center gap-1.5 text-ink-3">
                    <Hand className="size-3.5" />{a.kind === "gate" ? `Gate before ${STAGE_TITLE[a.stage as keyof typeof STAGE_TITLE] ?? titleCase(a.stage)}` : a.kind === "hardening" ? "Protections" : "Action"}
                  </span>
                </div>
                {a.status === "pending" ? <ApprovalBox approval={a} canDecide={canDecide} /> : (
                  <Card className="flex flex-wrap items-center gap-3 px-4 py-3">
                    <Badge tone={a.status === "approved" ? "ok" : a.status === "rejected" ? "crit" : "neutral"}>{titleCase(a.status)}</Badge>
                    <span className="text-[13.5px] font-semibold">{a.title}</span>
                    <span className="ml-auto text-[12.5px] text-ink-3">{personName(a.decided_by)} · <Time iso={a.decided_at} /></span>
                    {a.comment && <div className="w-full text-[12.5px] text-ink-2">“{a.comment}”</div>}
                  </Card>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
