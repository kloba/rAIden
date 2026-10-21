import {
  Activity, BellRing, BookOpenCheck, CalendarClock, ChevronDown, Cpu, Database, GraduationCap, Hand, LayoutDashboard, LogOut, MessageSquareText, Moon,
  ScrollText, Server, Settings, SlidersHorizontal, Sun, Zap,
} from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { NavLink, Outlet, useNavigate } from "react-router";
import { api } from "../lib/api";
import { useApprovals, useIncidents, useKnowledge, useLiveEvents, useMe } from "../lib/hooks";
import type { User } from "../lib/types";
import { Avatar, cn } from "./ui";

export function Brand({ size = "md" }: { size?: "md" | "lg" }) {
  return (
    <span className="inline-flex items-center gap-2">
      <span className={cn("inline-flex items-center justify-center rounded-lg bg-ink", size === "lg" ? "size-9" : "size-7")}>
        <svg viewBox="0 0 32 32" className={size === "lg" ? "size-6" : "size-5"} aria-hidden><path d="M18.5 3 8 18h7l-2 11 11-16h-7l1.5-10z" fill="var(--arc)" /></svg>
      </span>
      <span className={cn("display tracking-tight text-ink", size === "lg" ? "text-[26px]" : "text-[20px]")}>
        r<span className="text-arc">AI</span>den
      </span>
    </span>
  );
}

function NavItem({ to, icon, label, badge, tone }: { to: string; icon: ReactNode; label: string; badge?: number; tone?: "lamp" | "neutral" }) {
  return (
    <NavLink to={to} end={to === "/"}
      className={({ isActive }) => cn("group flex items-center gap-2.5 rounded-lg px-2.5 py-[7px] text-[13.5px] font-semibold transition-colors",
        isActive ? "bg-surface-3 text-ink" : "text-ink-2 hover:bg-surface-2 hover:text-ink")}>
      {({ isActive }) => (
        <>
          <span className={cn("shrink-0", isActive ? "text-arc" : "text-ink-3 group-hover:text-ink-2")}>{icon}</span>
          <span className="flex-1 truncate">{label}</span>
          {!!badge && (
            <span className={cn("min-w-5 rounded-full px-1.5 text-center text-[11px] font-bold leading-5",
              tone === "lamp" ? "bg-lamp text-[#2a1c00]" : "bg-surface-3 text-ink-2")}>{badge}</span>
          )}
        </>
      )}
    </NavLink>
  );
}

function Group({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="mt-4 first:mt-0">
      <div className="plate mb-1 px-2.5 text-[10.5px]">{label}</div>
      <div className="space-y-0.5">{children}</div>
    </div>
  );
}

function ThemeToggle() {
  const [theme, setTheme] = useState(() => document.documentElement.dataset.theme || "light");
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("raiden-theme", theme);
  }, [theme]);
  return (
    <button onClick={() => setTheme(theme === "dark" ? "light" : "dark")} aria-label={theme === "dark" ? "Switch to the day theme" : "Switch to the night theme"}
      title={theme === "dark" ? "Day shift" : "Night shift"} className="inline-flex size-8 items-center justify-center rounded-lg text-ink-2 hover:bg-surface-3 hover:text-ink">
      {theme === "dark" ? <Sun className="size-4" /> : <Moon className="size-4" />}
    </button>
  );
}

function UserMenu({ user }: { user: User }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const nav = useNavigate();
  useEffect(() => {
    const h = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, []);
  return (
    <div className="relative" ref={ref}>
      <button onClick={() => setOpen((v) => !v)} className="flex items-center gap-2 rounded-lg py-1 pr-1.5 pl-1 hover:bg-surface-3" aria-haspopup="menu" aria-expanded={open}>
        <Avatar name={user.name || user.email} size={28} />
        <span className="hidden text-left leading-tight md:block">
          <span className="block text-[13px] font-semibold text-ink">{user.name || user.email}</span>
          <span className="block text-[11.5px] text-ink-3 capitalize">{user.role}</span>
        </span>
        <ChevronDown className="size-3.5 text-ink-3" />
      </button>
      {open && (
        <div role="menu" className="rise absolute right-0 z-40 mt-1 w-56 rounded-xl border border-line bg-surface p-1.5 shadow-pop">
          <div className="px-2.5 py-1.5 text-[12px] text-ink-3">{user.email}</div>
          <button role="menuitem" onClick={async () => { await api.post("/auth/logout"); nav("/login"); }}
            className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-[13px] font-semibold text-ink-2 hover:bg-surface-3 hover:text-ink">
            <LogOut className="size-4" /> Sign out
          </button>
        </div>
      )}
    </div>
  );
}

/** The global lamp: violet while rAIden runs on its own, amber when somebody's decision is waiting. */
function AutopilotLamp({ waiting }: { waiting: number }) {
  const nav = useNavigate();
  return (
    <button onClick={() => nav(waiting ? "/approvals" : "/incidents")}
      className={cn("flex items-center gap-2 rounded-full border px-3 py-1.5 text-[12.5px] font-semibold transition-colors",
        waiting ? "border-lamp-line bg-lamp-soft text-lamp-ink hover:brightness-[0.98]" : "border-arc-line bg-arc-soft text-arc-ink")}>
      <span className={cn("inline-block size-2 rounded-full", waiting ? "lamp-glow bg-lamp" : "bg-arc")} />
      {waiting ? `${waiting} decision${waiting > 1 ? "s" : ""} waiting for you` : "Autopilot: nothing waiting for you"}
    </button>
  );
}

export function Shell({ demo }: { demo: boolean }) {
  const { data: user } = useMe();
  useLiveEvents(!!user);
  const approvals = useApprovals("pending");
  const incidents = useIncidents("open");
  const knowledge = useKnowledge();
  const waiting = approvals.data?.length ?? 0;
  const proposals = (knowledge.data?.learned_approvals.filter((la) => la.status === "proposed").length ?? 0) + (knowledge.data?.items.filter((k) => k.status === "proposed").length ?? 0);
  if (!user) return null;
  return (
    <div className="flex h-full min-h-0">
      <aside className="hidden w-[232px] shrink-0 flex-col border-r border-line bg-surface lg:flex">
        <div className="flex h-14 items-center px-4"><Brand /></div>
        <nav className="flex-1 overflow-y-auto px-2.5 pb-4">
          <Group label="Monitor">
            <NavItem to="/" icon={<LayoutDashboard className="size-4" />} label="Overview" />
            <NavItem to="/estate" icon={<Server className="size-4" />} label="Estate" />
            <NavItem to="/performance" icon={<Activity className="size-4" />} label="Performance" />
          </Group>
          <Group label="Respond">
            <NavItem to="/incidents" icon={<Zap className="size-4" />} label="Incidents" badge={incidents.data?.length} />
            <NavItem to="/approvals" icon={<Hand className="size-4" />} label="Needs you" badge={waiting} tone="lamp" />
            <NavItem to="/chat" icon={<MessageSquareText className="size-4" />} label="Investigate" />
          </Group>
          <Group label="Automate">
            <NavItem to="/routines" icon={<CalendarClock className="size-4" />} label="Routines" />
            <NavItem to="/monitors" icon={<BellRing className="size-4" />} label="Monitors" />
            <NavItem to="/knowledge" icon={<GraduationCap className="size-4" />} label="Knowledge" badge={proposals} tone="lamp" />
          </Group>
          <Group label="Configure">
            <NavItem to="/databases" icon={<Database className="size-4" />} label="Databases" />
            <NavItem to="/ai" icon={<Cpu className="size-4" />} label="AI endpoints" />
            <NavItem to="/autonomy" icon={<SlidersHorizontal className="size-4" />} label="Autonomy" />
            <NavItem to="/audit" icon={<ScrollText className="size-4" />} label="Audit log" />
            <NavItem to="/settings" icon={<Settings className="size-4" />} label="Settings" />
          </Group>
        </nav>
        <div className="border-t border-line px-4 py-3 text-[11.5px] text-ink-3">
          <a href="/api/docs" target="_blank" rel="noreferrer" className="inline-flex items-center gap-1.5 hover:text-ink-2"><BookOpenCheck className="size-3.5" /> API reference</a>
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center gap-3 border-b border-line bg-surface/80 px-4 backdrop-blur lg:px-6">
          <div className="lg:hidden"><Brand /></div>
          {demo && (
            <span className="rounded-md border border-dashed border-line-strong px-2 py-0.5 text-[11.5px] font-semibold text-ink-3"
              title="Simulated servers and a scripted model: nothing real is contacted">Demo fleet · simulated</span>
          )}
          <div className="flex-1" />
          <AutopilotLamp waiting={waiting} />
          <ThemeToggle />
          <UserMenu user={user} />
        </header>
        <main className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-[1500px] px-4 py-6 lg:px-8">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}
