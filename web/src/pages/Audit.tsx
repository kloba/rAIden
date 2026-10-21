import { Bot, Search, User as UserIcon } from "lucide-react";
import { useState } from "react";
import { Badge, Button, Card, EmptyState, Input, Loading, PageHeader, Time } from "../components/ui";
import { api } from "../lib/api";
import { personName } from "../lib/format";
import { useAudit } from "../lib/hooks";
import type { AuditEntry } from "../lib/types";

function detailText(d: Record<string, unknown>) {
  return Object.entries(d).filter(([, v]) => v !== null && v !== "" && !(Array.isArray(v) && !v.length))
    .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`).join("  ");
}

export function AuditPage() {
  const [actor, setActor] = useState("");
  const [action, setAction] = useState("");
  const [target, setTarget] = useState("");
  const [older, setOlder] = useState<AuditEntry[]>([]);
  const [loadingMore, setLoadingMore] = useState(false);
  const q = new URLSearchParams({ limit: "200" });
  if (actor) q.set("actor", actor);
  if (action) q.set("action", action);
  if (target) q.set("target", target);
  const audit = useAudit(q.toString());
  const rows = [...(audit.data ?? []), ...older];
  const more = async () => {
    const last = rows[rows.length - 1];
    if (!last) return;
    setLoadingMore(true);
    const p = new URLSearchParams(q);
    p.set("before", String(last.id));
    try {
      const next = await api.get<AuditEntry[]>(`/audit?${p.toString()}`);
      setOlder((o) => [...o, ...next]);
    } finally {
      setLoadingMore(false);
    }
  };
  const reset = (f: () => void) => { setOlder([]); f(); };
  return (
    <div>
      <PageHeader title="Audit log" sub="Every change and every action, by people and by rAIden: who, what, on which server, and whether it was allowed. Append-only." />
      <div className="mb-3 flex flex-wrap gap-2">
        <div className="relative"><Search className="pointer-events-none absolute top-2.5 left-2.5 size-4 text-ink-3" /><Input value={actor} onChange={(e) => reset(() => setActor(e.target.value))} placeholder="Who" className="w-48 pl-8" /></div>
        <Input value={action} onChange={(e) => reset(() => setAction(e.target.value))} placeholder="Action, e.g. action. or autonomy" className="w-64" />
        <Input value={target} onChange={(e) => reset(() => setTarget(e.target.value))} placeholder="Server or object" className="w-48" />
      </div>
      <Card className="overflow-hidden">
        {audit.isLoading ? <Loading /> : rows.length === 0 ? <EmptyState title="Nothing matches" /> : (
          <div className="overflow-x-auto" tabIndex={0} role="region" aria-label="Audit log">
            <table className="w-full min-w-[960px] text-left text-[12.5px]">
              <thead className="bg-surface-2 text-[11.5px] text-ink-3"><tr>{["When", "Who", "What", "On", "Result", "Details"].map((h) => <th key={h} className="px-3 py-2 font-semibold">{h}</th>)}</tr></thead>
              <tbody>
                {rows.map((a) => (
                  <tr key={a.id} className="border-t border-line align-top hover:bg-surface-2">
                    <td className="px-3 py-2 whitespace-nowrap text-ink-2 num"><Time iso={a.ts} mode="date" /></td>
                    <td className="px-3 py-2 whitespace-nowrap">
                      <span className="inline-flex items-center gap-1.5 font-semibold">
                        {a.actor_type === "raiden" || a.actor === "raiden" || a.actor === "autopilot" ? <Bot className="size-3.5 text-arc-ink" /> : <UserIcon className="size-3.5 text-ink-3" />}
                        {personName(a.actor)}
                      </span>
                    </td>
                    <td className="px-3 py-2 font-mono text-[12px] whitespace-nowrap">{a.action}</td>
                    <td className="px-3 py-2 whitespace-nowrap">{a.target || "—"}</td>
                    <td className="px-3 py-2"><Badge tone={a.decision === "allow" ? "ok" : a.decision === "deny" ? "crit" : a.decision === "error" ? "warn" : "neutral"}>{a.decision}</Badge></td>
                    <td className="max-w-[520px] px-3 py-2 font-mono text-[11.5px] break-words text-ink-3">{detailText(a.detail)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {(audit.data?.length ?? 0) >= 200 && (
          <div className="flex justify-center border-t border-line py-2.5"><Button size="sm" variant="ghost" onClick={more} loading={loadingMore}>Show older entries</Button></div>
        )}
      </Card>
    </div>
  );
}
