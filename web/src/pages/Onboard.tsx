import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, Check, CheckCircle2, CircleAlert, FlaskConical, PlugZap, XCircle } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router";
import { AssessmentView, CopyBlock } from "../components/assessment";
import { AgentToken, ServerAutonomy } from "../components/autonomy";
import {
  ConnectionFields, DescribeFields, draftBody, emptyDraft, PLATFORM_CARDS, preparation, slugify, type ServerDraft,
} from "../components/serverform";
import { Badge, Button, Card, CardHeader, cn, ErrorNote, Loading, PageHeader, Segmented } from "../components/ui";
import { api } from "../lib/api";
import { categoryLabel } from "../lib/format";
import { useAssessment, useMe, useMonitors, useRoutines } from "../lib/hooks";
import type { AuthStatus, Server } from "../lib/types";

const STEPS = ["Where it runs", "Connect", "Describe", "Assess and tune", "Autonomy and automation"];

interface TestResult {
  ok: boolean;
  message: string;
  latency_ms?: number;
  in_recovery?: boolean;
  checks: { name: string; ok: boolean; detail: string }[];
}

const CHECK_TITLE: Record<string, string> = {
  connect: "Connection",
  pg_monitor: "Statistics access (pg_monitor)",
  pg_stat_statements: "Query statistics (pg_stat_statements)",
  signal_backend: "Cancel and end sessions",
  create_role: "Cap roles (connection limits, timeouts)",
};
const REQUIRED = new Set(["connect", "pg_monitor"]);

function Stepper({ step }: { step: number }) {
  return (
    <ol className="mb-6 flex flex-wrap items-center gap-x-2 gap-y-2">
      {STEPS.map((s, i) => (
        <li key={s} className="flex items-center gap-2">
          <span className={cn("flex size-6 items-center justify-center rounded-full text-[12px] font-bold",
            i < step ? "bg-arc text-on-arc" : i === step ? "bg-ink text-surface" : "border border-line-strong text-ink-3")}>
            {i < step ? <Check className="size-3.5" /> : i + 1}
          </span>
          <span className={cn("text-[13px] font-semibold", i === step ? "text-ink" : "text-ink-3")}>{s}</span>
          {i < STEPS.length - 1 && <span className="mx-1 h-px w-6 bg-line-strong" aria-hidden />}
        </li>
      ))}
    </ol>
  );
}

/** Demo only: the simulated server onboarding can connect to. Nothing real is contacted. */
function DemoCandidate({ c, onUse }: { c: NonNullable<AuthStatus["demo_onboarding"]>; onUse: () => void }) {
  return (
    <div className="mb-4 flex flex-wrap items-center gap-3 rounded-xl border border-dashed border-arc/40 bg-arc-soft/40 px-3.5 py-3">
      <FlaskConical className="size-5 shrink-0 text-arc-ink" />
      <div className="min-w-0 flex-1 text-[12.5px] text-ink-2">
        <span className="font-semibold text-ink">Try it on the demo fleet.</span> <span className="font-mono text-[12px]">{c.host}</span> is a simulated {c.version} that
        is not in the estate yet: {c.description.charAt(0).toLowerCase() + c.description.slice(1)}
      </div>
      <Button size="sm" onClick={onUse}>Use the demo server</Button>
    </div>
  );
}

function TestPanel({ result }: { result: TestResult }) {
  return (
    <div className={cn("rounded-xl border p-3.5", result.ok ? "border-ok/30 bg-ok-soft/50" : "border-crit/30 bg-crit-soft/60")}>
      <div className={cn("flex items-center gap-2 text-[13.5px] font-semibold", result.ok ? "text-ok" : "text-crit")}>
        {result.ok ? <CheckCircle2 className="size-4" /> : <XCircle className="size-4" />} {result.message}
        {result.in_recovery && <Badge>Standby</Badge>}
      </div>
      {result.checks.length > 0 && (
        <ul className="mt-2.5 space-y-1.5">
          {result.checks.map((c) => (
            <li key={c.name} className="flex items-start gap-2 text-[12.5px]">
              {c.ok ? <Check className="mt-0.5 size-3.5 shrink-0 text-ok" /> : REQUIRED.has(c.name) ? <XCircle className="mt-0.5 size-3.5 shrink-0 text-crit" /> : <CircleAlert className="mt-0.5 size-3.5 shrink-0 text-warn" />}
              <span><span className="font-semibold text-ink">{CHECK_TITLE[c.name] ?? c.name}</span>{!REQUIRED.has(c.name) && !c.ok && <span className="text-ink-3"> (optional)</span>}: <span className="text-ink-2">{c.detail}</span></span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function OnboardPage() {
  const [step, setStep] = useState(0);
  const [d, setD] = useState<ServerDraft>(emptyDraft());
  const set = (p: Partial<ServerDraft>) => setD((x) => ({ ...x, ...p }));
  const [test, setTest] = useState<TestResult | null>(null);
  const [server, setServer] = useState<Server | null>(null);
  const [assessmentId, setAssessmentId] = useState<number | null>(null);
  const me = useMe();
  const status = useQuery({ queryKey: ["auth-status"], queryFn: () => api.get<AuthStatus>("/auth/status"), staleTime: 60_000 });
  const demo = status.data?.demo_onboarding;
  const nav = useNavigate();
  const qc = useQueryClient();
  const assessment = useAssessment(assessmentId);
  const monitors = useMonitors();
  const routines = useRoutines();
  const prep = preparation(d.platform);

  const runTest = useMutation({
    mutationFn: () => api.post<TestResult>("/servers/test", { host: d.host.trim(), port: d.port, dbname: d.dbname, username: d.username, password: d.password || null, sslmode: d.sslmode }),
    onSuccess: setTest,
    onError: (e) => setTest({ ok: false, message: (e as Error).message, checks: [] }),
  });
  const create = useMutation({
    mutationFn: async () => {
      const srv = server ?? (await api.post<Server>("/servers", draftBody(d, true)));
      setServer(srv);
      const a = await api.post<{ id: number }>(`/servers/${srv.id}/assess?trigger=onboarding`);
      return { srv, a };
    },
    onSuccess: ({ a }) => {
      setAssessmentId(a.id);
      setStep(3);
      for (const k of ["servers", "estate", "overview"]) qc.invalidateQueries({ queryKey: [k] });
    },
  });
  const rerun = useMutation({
    mutationFn: () => api.post<{ id: number }>(`/servers/${server!.id}/assess?trigger=onboarding`),
    onSuccess: (a) => setAssessmentId(a.id),
  });

  const myMonitors = useMemo(() => (monitors.data ?? []).filter((m) => m.enabled && (!m.scope.environments?.length || m.scope.environments.includes(d.environment))
    && (!m.scope.role || (m.scope.role === "replica") === !!test?.in_recovery)), [monitors.data, d.environment, test]);
  const myRoutines = useMemo(() => (routines.data ?? []).filter((r) => r.enabled && (!r.scope.environments?.length || r.scope.environments.includes(d.environment))), [routines.data, d.environment]);
  const categories = useMemo(() => {
    const c = new Map<string, number>();
    for (const m of myMonitors) c.set(m.category, (c.get(m.category) ?? 0) + 1);
    return [...c.entries()].sort((a, b) => b[1] - a[1]);
  }, [myMonitors]);

  if (me.data && me.data.role !== "admin") {
    return <Card className="p-6 text-[13.5px] text-ink-2">Only an admin can add databases. Ask an admin of this console to onboard the server.</Card>;
  }
  const canConnect = d.host.trim() && d.username.trim();
  const platform = PLATFORM_CARDS.find((p) => p.id === d.platform);

  return (
    <div className="mx-auto max-w-[1180px]">
      <Link to="/databases" className="mb-3 inline-flex items-center gap-1.5 text-[13px] font-semibold text-ink-3 hover:text-ink"><ArrowLeft className="size-4" /> Databases</Link>
      <PageHeader title="Add a database" sub="Connect any PostgreSQL 13 or newer. rAIden checks its configuration, sizing, security and maintenance, proposes fixes, and starts watching it." />
      <Stepper step={step} />

      {step === 0 && (
        <Card className="p-5">
          <div className="plate mb-3">Where does it run?</div>
          <div className="grid gap-2.5 sm:grid-cols-2 lg:grid-cols-4">
            {PLATFORM_CARDS.map((p) => (
              <button key={p.id} onClick={() => set({ platform: p.id })} aria-pressed={d.platform === p.id}
                className={cn("flex items-start gap-3 rounded-xl border p-3.5 text-left transition-colors",
                  d.platform === p.id ? "border-arc bg-arc-soft/60 ring-2 ring-arc/20" : "border-line bg-surface hover:border-line-strong hover:bg-surface-2")}>
                <span className={cn("mt-0.5 shrink-0", d.platform === p.id ? "text-arc-ink" : "text-ink-3")}>{p.icon}</span>
                <span><span className="block text-[14px] font-semibold text-ink">{p.title}</span><span className="block text-[12.5px] text-ink-3">{p.hint}</span></span>
              </button>
            ))}
          </div>
          <div className="mt-6 plate mb-2">Environment</div>
          <Segmented value={d.environment} onChange={(v) => set({ environment: v })} options={[{ id: "production", label: "Production" }, { id: "staging", label: "Staging" }, { id: "development", label: "Development" }]} />
          <p className="mt-2 max-w-2xl text-[12.5px] text-ink-3">
            {d.environment === "production" ? "Production: rAIden investigates on its own, but a person approves every fix and every new protection."
              : d.environment === "staging" ? "Staging: rAIden fixes on its own; a person approves new protections."
              : "Development: rAIden runs every stage on autopilot."} High-risk actions always wait for a person. You can change this per server later.
          </p>
          <div className="mt-6 flex justify-end"><Button variant="primary" onClick={() => setStep(1)}>Continue <ArrowRight className="size-4" /></Button></div>
        </Card>
      )}

      {step === 1 && (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_440px]">
          <Card className="p-5">
            <div className="plate mb-3">Connection</div>
            {demo && d.host !== demo.host && (
              <DemoCandidate c={demo} onUse={() => {
                setTest(null);
                set({
                  host: demo.host, port: demo.port, dbname: demo.dbname, username: demo.username, password: "demo-password", sslmode: "require",
                  platform: demo.platform, name: demo.name, id: slugify(demo.name), owner: demo.owner, description: demo.description, tags: demo.tags,
                  maintenance_window: demo.maintenance_window, ram_gb: String(demo.ram_gb), vcpus: String(demo.vcpus), storage: demo.storage, workload: demo.workload,
                });
              }} />
            )}
            <ConnectionFields d={d} set={(p) => { set(p); setTest(null); }} />
            <div className="mt-4 flex flex-wrap items-center gap-2">
              <Button onClick={() => runTest.mutate()} loading={runTest.isPending} disabled={!canConnect}><PlugZap className="size-4" /> Test connection</Button>
              <span className="text-[12.5px] text-ink-3">Read-only checks: nothing is changed on the server.</span>
            </div>
            {test && <div className="mt-4"><TestPanel result={test} /></div>}
            <div className="mt-6 flex items-center justify-between gap-2">
              <Button variant="ghost" onClick={() => setStep(0)}><ArrowLeft className="size-4" /> Back</Button>
              <div className="flex items-center gap-2">
                {!test?.ok && canConnect && <Button variant="ghost" onClick={() => { if (!d.name) set({ name: d.host.split(".")[0]!, id: slugify(d.host.split(".")[0]!) }); setStep(2); }}>Add without a successful test</Button>}
                <Button variant="primary" disabled={!test?.ok} onClick={() => { if (!d.name) set({ name: d.host.split(".")[0]!, id: slugify(d.host.split(".")[0]!) }); setStep(2); }}>Continue <ArrowRight className="size-4" /></Button>
              </div>
            </div>
          </Card>
          <Card>
            <CardHeader title={<span className="inline-flex items-center gap-2">{platform?.icon} Prepare {platform?.title ?? "the server"}</span>} sub="A login that can read statistics and nothing else. Actions are opt-in." />
            <ol className="space-y-4 px-4 pb-4">
              {prep.steps.map((s, i) => (
                <li key={i}>
                  <div className="text-[13px] font-semibold"><span className="mr-1.5 text-ink-3 num">{i + 1}.</span>{s.title}</div>
                  {s.text && <div className="mt-0.5 text-[12.5px] text-ink-2">{s.text}</div>}
                  {s.code && <CopyBlock text={s.code} />}
                </li>
              ))}
            </ol>
            {prep.note && <div className="mx-4 mb-4 rounded-lg bg-surface-2 px-3 py-2 text-[12px] text-ink-3">{prep.note}</div>}
          </Card>
        </div>
      )}

      {step === 2 && (
        <Card className="p-5">
          <DescribeFields d={d} set={set} isNew={!server} />
          {create.error && <div className="mt-4"><ErrorNote error={create.error} /></div>}
          <div className="mt-6 flex items-center justify-between gap-2">
            <Button variant="ghost" onClick={() => setStep(1)} disabled={!!server}><ArrowLeft className="size-4" /> Back</Button>
            <Button variant="primary" onClick={() => create.mutate()} loading={create.isPending} disabled={!d.name.trim() || !d.id}>
              Add and assess <ArrowRight className="size-4" />
            </Button>
          </div>
        </Card>
      )}

      {step === 3 && server && (
        <div>
          {assessment.data ? (
            <AssessmentView assessment={assessment.data} serverId={server.id} serverName={server.name} canApply onRerun={() => rerun.mutate()} rerunning={rerun.isPending} />
          ) : <Loading label="Starting the assessment…" />}
          <div className="mt-5 flex items-center justify-between">
            <span className="text-[12.5px] text-ink-3">Findings you do not fix now stay on the server as issues and suggestions.</span>
            <Button variant="primary" disabled={assessment.data?.status === "running"} onClick={() => setStep(4)}>Continue <ArrowRight className="size-4" /></Button>
          </div>
        </div>
      )}

      {step === 4 && server && (
        <div className="grid gap-4 lg:grid-cols-2">
          <Card className="lg:row-span-2">
            <CardHeader title="Autonomy for this server" sub={`Each gate follows the ${server.environment} policy unless you pin it here.`} />
            <div className="px-4 pb-3"><ServerAutonomy server={server} canEdit /></div>
          </Card>
          <Card>
            <CardHeader title="Watching it" sub={`${myMonitors.length} monitors apply to this server, evaluated every collection.`} />
            <div className="flex flex-wrap gap-1.5 px-4 pb-4">
              {categories.map(([c, n]) => <Badge key={c}>{categoryLabel(c)} <span className="text-ink-3 num">{n}</span></Badge>)}
            </div>
            <div className="border-t border-line px-4 py-3">
              <div className="plate mb-1.5">Routines that include it</div>
              <ul className="space-y-1 text-[12.5px]">
                {myRoutines.map((r) => <li key={r.id} className="flex justify-between gap-3"><span className="font-semibold">{r.name}</span><span className="text-ink-3">{r.schedule}</span></li>)}
              </ul>
            </div>
          </Card>
          {["self_managed", "vm", "kubernetes", "other"].includes(server.platform) && (
            <Card>
              <CardHeader title="Host agent (recommended)" />
              <div className="px-4 pb-4"><AgentToken server={server} canEdit /></div>
            </Card>
          )}
          <div className="flex items-center justify-end gap-2 lg:col-span-2">
            <Button variant="primary" onClick={() => nav(`/servers/${server.id}`)}>Open {server.name} <ArrowRight className="size-4" /></Button>
          </div>
        </div>
      )}
    </div>
  );
}
