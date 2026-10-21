import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Navigate, useNavigate } from "react-router";
import { Brand } from "../components/Shell";
import { Button, ErrorNote, Field, Input } from "../components/ui";
import { api } from "../lib/api";
import type { AuthStatus, User } from "../lib/types";

function Frame({ children, side }: { children: React.ReactNode; side: React.ReactNode }) {
  return (
    <div className="grid min-h-full lg:grid-cols-[1fr_minmax(420px,520px)]">
      <div className="relative hidden overflow-hidden bg-ink lg:block">
        <CircuitArt />
        <div className="relative flex h-full flex-col justify-between p-12 text-white">
          <span className="inline-flex items-center gap-2">
            <svg viewBox="0 0 32 32" className="size-8" aria-hidden><path d="M18.5 3 8 18h7l-2 11 11-16h-7l1.5-10z" fill="var(--arc)" /></svg>
            <span className="display text-[24px]">r<span style={{ color: "#a59cff" }}>AI</span>den Console</span>
          </span>
          <div className="max-w-md">{side}</div>
          <div className="text-[12px] text-white/50">PostgreSQL on-prem, on VMs, in Kubernetes, on RDS, Cloud SQL and Azure.</div>
        </div>
      </div>
      <div className="flex items-center justify-center bg-surface px-6 py-12">{children}</div>
    </div>
  );
}

/** The incident circuit, drawn large: current flows through closed breakers and stops at the one waiting for a person. */
function CircuitArt() {
  const stages = ["Detect", "Triage", "Diagnose", "Remediate", "Verify", "Harden", "Resolve"];
  return (
    <svg className="absolute inset-0 h-full w-full opacity-[0.55]" viewBox="0 0 700 900" preserveAspectRatio="xMidYMid slice" aria-hidden>
      {stages.map((s, i) => {
        const y = 140 + i * 100;
        const open = i === 3;
        return (
          <g key={s}>
            <line x1={160} x2={160} y1={y} y2={y + 100} stroke={open ? "#5a4a1c" : "#8e83ff"} strokeWidth={2} className={open ? "" : "current"} />
            <circle cx={160} cy={y} r={6} fill={open ? "#ffbd2e" : "#8e83ff"} className={open ? "lamp-glow" : ""} />
            <text x={186} y={y + 5} fill="white" fillOpacity={0.6} fontSize={15} fontFamily="Barlow Semi Condensed" letterSpacing="2">{s.toUpperCase()}</text>
          </g>
        );
      })}
    </svg>
  );
}

export function LoginPage() {
  const qc = useQueryClient();
  const nav = useNavigate();
  const status = useQuery({ queryKey: ["auth-status"], queryFn: () => api.get<AuthStatus>("/auth/status") });
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  if (status.data?.needs_setup) return <Navigate to="/setup" replace />;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const user = await api.post<User>("/auth/login", { email, password });
      qc.setQueryData(["me"], user);
      nav("/", { replace: true });
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  const demo = status.data?.demo_login;
  return (
    <Frame side={<>
      <p className="display text-[34px] leading-[1.1]">Your on-call DBA never sleeps.<br /><span style={{ color: "#a59cff" }}>You decide where it needs you.</span></p>
      <p className="mt-4 text-[15px] text-white/70">rAIden watches every PostgreSQL server, works each incident from detection to a hardened fix, and stops at the gates you choose for a person's approval.</p>
    </>}>
      <form onSubmit={submit} className="w-full max-w-sm space-y-4">
        <div className="lg:hidden"><Brand size="lg" /></div>
        <div>
          <h1 className="display text-[26px]">Sign in</h1>
          <p className="text-[13.5px] text-ink-3">to the rAIden Console</p>
        </div>
        <Field label="Email"><Input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus /></Field>
        <Field label="Password"><Input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></Field>
        <ErrorNote error={error} />
        <Button variant="primary" className="w-full" loading={busy} type="submit">Sign in</Button>
        {demo && (
          <div className="rounded-lg border border-dashed border-line-strong bg-surface-2 px-3 py-2.5 text-[12.5px] text-ink-2">
            <div className="font-semibold text-ink">Demo fleet</div>
            Simulated servers and a scripted model. Sign in as <span className="mono">{demo.email}</span> / <span className="mono">{demo.password}</span>{" "}
            <button type="button" className="font-semibold text-arc-ink underline underline-offset-2" onClick={() => { setEmail(demo.email); setPassword(demo.password); }}>fill in</button>
          </div>
        )}
      </form>
    </Frame>
  );
}

export function SetupPage() {
  const qc = useQueryClient();
  const nav = useNavigate();
  const [form, setForm] = useState({ name: "", email: "", password: "" });
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const user = await api.post<User>("/auth/setup", form);
      qc.setQueryData(["me"], user);
      nav("/databases/new", { replace: true });
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Frame side={<>
      <p className="display text-[34px] leading-[1.1]">Welcome. Three steps<br />to your first watched server.</p>
      <ol className="mt-5 space-y-2 text-[15px] text-white/75">
        <li>1 · Create the administrator account</li>
        <li>2 · Connect a PostgreSQL server (any platform)</li>
        <li>3 · Connect an AI endpoint, or start with checks only</li>
      </ol>
    </>}>
      <form onSubmit={submit} className="w-full max-w-sm space-y-4">
        <div>
          <h1 className="display text-[26px]">Create the administrator</h1>
          <p className="text-[13.5px] text-ink-3">This account can add servers, AI endpoints and other people.</p>
        </div>
        <Field label="Your name"><Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} autoFocus /></Field>
        <Field label="Email"><Input type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} required /></Field>
        <Field label="Password" hint="At least 10 characters."><Input type="password" autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} required /></Field>
        <ErrorNote error={error} />
        <Button variant="primary" className="w-full" loading={busy} type="submit">Create account</Button>
      </form>
    </Frame>
  );
}
