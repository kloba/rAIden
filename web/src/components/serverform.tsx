import { Boxes, CircleDashed, Cloud, HardDrive, Server as ServerIcon } from "lucide-react";
import type { ReactNode } from "react";
import type { Env, Server } from "../lib/types";
import { Field, Input, Segmented, Select, Textarea } from "./ui";

export interface ServerDraft {
  id: string;
  name: string;
  environment: Env;
  platform: string;
  host: string;
  port: number;
  dbname: string;
  username: string;
  password: string;
  sslmode: string;
  databases: string;
  azure_resource_id: string;
  tags: string;
  owner: string;
  description: string;
  maintenance_window: string;
  collect_interval_s: number;
  enabled: boolean;
  ram_gb: string;
  vcpus: string;
  storage: string;
  workload: string;
}

export const emptyDraft = (): ServerDraft => ({
  id: "", name: "", environment: "production", platform: "self_managed", host: "", port: 5432, dbname: "postgres", username: "raiden_monitor", password: "",
  sslmode: "prefer", databases: "", azure_resource_id: "", tags: "", owner: "", description: "", maintenance_window: "", collect_interval_s: 60, enabled: true,
  ram_gb: "", vcpus: "", storage: "ssd", workload: "oltp",
});

export function draftFrom(s: Server): ServerDraft {
  return {
    id: s.id, name: s.name, environment: s.environment, platform: s.platform, host: s.host, port: s.port, dbname: s.dbname, username: s.username, password: "",
    sslmode: s.sslmode, databases: s.databases.join(", "), azure_resource_id: s.azure_resource_id ?? "", tags: Object.entries(s.tags).map(([k, v]) => `${k}=${v}`).join(", "),
    owner: s.owner, description: s.description, maintenance_window: s.maintenance_window, collect_interval_s: s.collect_interval_s, enabled: s.enabled,
    ram_gb: s.host_info.ram_gb ? String(s.host_info.ram_gb) : "", vcpus: s.host_info.vcpus ? String(s.host_info.vcpus) : "",
    storage: s.host_info.storage ?? "ssd", workload: s.host_info.workload ?? "oltp",
  };
}

export const slugify = (s: string) => s.toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 63);

/** The request body for POST /servers (new) or PATCH /servers/{id}: an empty password means "keep the stored one". */
export function draftBody(d: ServerDraft, isNew: boolean): Record<string, unknown> {
  const tags = Object.fromEntries(d.tags.split(",").map((t) => t.trim()).filter(Boolean).map((t) => {
    const [k, ...v] = t.split("=");
    return [k!.trim(), v.join("=").trim()];
  }));
  const body: Record<string, unknown> = {
    name: d.name.trim(), environment: d.environment, platform: d.platform, host: d.host.trim(), port: Number(d.port) || 5432, dbname: d.dbname.trim() || "postgres",
    username: d.username.trim(), sslmode: d.sslmode, databases: d.databases.split(",").map((x) => x.trim()).filter(Boolean),
    azure_resource_id: d.azure_resource_id.trim() || null, tags, owner: d.owner.trim(), description: d.description.trim(), maintenance_window: d.maintenance_window.trim(),
    collect_interval_s: Number(d.collect_interval_s) || 60, enabled: d.enabled,
    host_info: { ram_gb: d.ram_gb ? Number(d.ram_gb) : null, vcpus: d.vcpus ? Number(d.vcpus) : null, storage: d.storage || null, workload: d.workload || null },
  };
  if (isNew) body.id = d.id || slugify(d.name);
  if (d.password || isNew) body.password = d.password || null;
  return body;
}

export const PLATFORM_CARDS: { id: string; title: string; hint: string; icon: ReactNode }[] = [
  { id: "self_managed", title: "On-prem", hint: "Bare metal or your own data center", icon: <ServerIcon className="size-5" /> },
  { id: "vm", title: "Virtual machine", hint: "PostgreSQL you run on any cloud or hypervisor VM", icon: <HardDrive className="size-5" /> },
  { id: "kubernetes", title: "Kubernetes", hint: "CloudNativePG, Zalando, Crunchy or StatefulSets", icon: <Boxes className="size-5" /> },
  { id: "azure_flexible", title: "Azure", hint: "Azure Database for PostgreSQL flexible server", icon: <Cloud className="size-5" /> },
  { id: "aws_rds", title: "Amazon RDS", hint: "RDS for PostgreSQL", icon: <Cloud className="size-5" /> },
  { id: "aws_aurora", title: "Amazon Aurora", hint: "Aurora PostgreSQL-compatible", icon: <Cloud className="size-5" /> },
  { id: "gcp_cloudsql", title: "Google Cloud SQL", hint: "Cloud SQL for PostgreSQL", icon: <Cloud className="size-5" /> },
  { id: "other", title: "Something else", hint: "Any server that speaks the PostgreSQL protocol (13+)", icon: <CircleDashed className="size-5" /> },
];

// comments on their own lines: the snippets sit in a narrow column
const ACT = `-- optional: cancel or end sessions when
-- the autonomy policy lets rAIden act
GRANT pg_signal_backend TO raiden_monitor;`;

/** How to prepare the monitoring login on each platform: least privilege first, actions opt-in. */
export function preparation(platform: string): { steps: { title: string; code?: string; text?: string }[]; note?: string } {
  const login = `-- reads every statistic, nothing else
CREATE ROLE raiden_monitor LOGIN
  CONNECTION LIMIT 5
  PASSWORD '<a strong password>';
GRANT pg_monitor TO raiden_monitor;`;
  switch (platform) {
    case "azure_flexible":
      return {
        steps: [
          { title: "Allow the console to connect", text: "Add the console's address to the server's firewall rules, or reach it through a private endpoint." },
          { title: "Create the login (as the admin user)", code: `${login}\n\n${ACT}` },
          { title: "Turn on query statistics", text: "pg_stat_statements is preloaded by default. In Server parameters, allow PG_STAT_STATEMENTS in azure.extensions, then:", code: "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;" },
          { title: "Optional: platform metrics", text: "Paste the server's resource ID in the next step and give the console's identity the Monitoring Reader role: CPU, memory, IOPS and storage then come from Azure Monitor." },
        ],
      };
    case "aws_rds":
    case "aws_aurora":
      return {
        steps: [
          { title: "Allow the console to connect", text: "Open port 5432 in the instance's security group for the console's address." },
          { title: "Create the login (as the master user)", code: `${login}\n\n${ACT}` },
          { title: "Turn on query statistics", text: "pg_stat_statements is preloaded by the default parameter group; create it in the database:", code: "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;" },
        ],
        note: "Host CPU and memory are not visible over SQL on managed services: rAIden reads everything PostgreSQL itself reports.",
      };
    case "gcp_cloudsql":
      return {
        steps: [
          { title: "Allow the console to connect", text: "Use private IP, an authorized network, or the Cloud SQL Auth Proxy next to the console." },
          { title: "Create the login (as the postgres user)", code: `${login}\n\n${ACT}` },
          { title: "Turn on query statistics", code: "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;" },
        ],
        note: "Host CPU and memory are not visible over SQL on managed services: rAIden reads everything PostgreSQL itself reports.",
      };
    case "kubernetes":
      return {
        steps: [
          { title: "Declare the login (CloudNativePG example)", code: `spec:
  postgresql:
    shared_preload_libraries: [pg_stat_statements]
  managed:
    roles:
      - name: raiden_monitor
        login: true
        connectionLimit: 5
        inRoles: [pg_monitor, pg_signal_backend]
        passwordSecret: { name: raiden-monitor }` },
          { title: "Turn on query statistics", code: "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;" },
          { title: "Connect through the read-write service", text: "Point the console at <cluster>-rw.<namespace>.svc (or a LoadBalancer) on port 5432." },
        ],
      };
    default:
      return {
        steps: [
          { title: "Create the login (as a superuser)", code: `${login}\n\n${ACT}
-- optional: VACUUM, ANALYZE, REINDEX (17+)
GRANT pg_maintain TO raiden_monitor;` },
          { title: "Turn on query statistics", text: "Add pg_stat_statements to shared_preload_libraries in postgresql.conf, restart once, then:", code: "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;" },
          { title: "Allow the connection", text: "Add a line to pg_hba.conf for the console's address, for example:", code: "hostssl all raiden_monitor 10.0.0.0/8 scram-sha-256" },
          { title: "Optional: the host agent", text: "Install the rAIden host agent on the database host for CPU, memory, disk and log-driven incidents (crashes, corruption, dropped tables). You get its token after the server is added." },
        ],
      };
  }
}

type Set_ = (patch: Partial<ServerDraft>) => void;

export function ConnectionFields({ d, set, editing }: { d: ServerDraft; set: Set_; editing?: boolean }) {
  return (
    <div className="grid gap-3 sm:grid-cols-6">
      <Field label="Host" className="sm:col-span-4" hint="Name or address the console can reach">
        <Input value={d.host} onChange={(e) => set({ host: e.target.value })} placeholder="pg-orders-01.internal" autoComplete="off" spellCheck={false} />
      </Field>
      <Field label="Port" className="sm:col-span-2"><Input type="number" value={d.port} onChange={(e) => set({ port: Number(e.target.value) })} /></Field>
      <Field label="Database" className="sm:col-span-2"><Input value={d.dbname} onChange={(e) => set({ dbname: e.target.value })} spellCheck={false} /></Field>
      <Field label="Login" className="sm:col-span-2"><Input value={d.username} onChange={(e) => set({ username: e.target.value })} autoComplete="off" spellCheck={false} /></Field>
      <Field label="Password" className="sm:col-span-2" hint={editing ? "Leave empty to keep the stored password" : "Encrypted at rest"}>
        <Input type="password" value={d.password} onChange={(e) => set({ password: e.target.value })} autoComplete="new-password" placeholder={editing ? "••••••••" : ""} />
      </Field>
      <Field label="TLS" className="sm:col-span-3" hint="verify-full checks the certificate and the host name">
        <Select value={d.sslmode} onChange={(e) => set({ sslmode: e.target.value })}>
          {["require", "verify-full", "verify-ca", "prefer", "allow", "disable"].map((m) => <option key={m} value={m}>{m}</option>)}
        </Select>
      </Field>
      <Field label="Databases to watch" className="sm:col-span-3" hint="Comma separated; empty means every database">
        <Input value={d.databases} onChange={(e) => set({ databases: e.target.value })} placeholder="orders, billing" spellCheck={false} />
      </Field>
    </div>
  );
}

export function DescribeFields({ d, set, isNew }: { d: ServerDraft; set: Set_; isNew: boolean }) {
  return (
    <div className="space-y-5">
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Display name"><Input value={d.name} onChange={(e) => set({ name: e.target.value, ...(isNew ? { id: slugify(e.target.value) } : {}) })} placeholder="orders-prod" /></Field>
        <Field label="ID" hint={isNew ? "Used in URLs and the API; cannot change later" : "Cannot change"}>
          <Input value={d.id} disabled={!isNew} onChange={(e) => set({ id: slugify(e.target.value) })} spellCheck={false} className="font-mono" />
        </Field>
        <Field label="Environment" hint="Decides the default autonomy: production keeps a person on fixes and protections.">
          <Segmented value={d.environment} onChange={(v) => set({ environment: v })} options={[{ id: "production", label: "Production" }, { id: "staging", label: "Staging" }, { id: "development", label: "Development" }]} />
        </Field>
        <Field label="Owner" hint="Team or person rAIden names in reports"><Input value={d.owner} onChange={(e) => set({ owner: e.target.value })} placeholder="team-checkout" /></Field>
        <Field label="Tags" hint="key=value, comma separated"><Input value={d.tags} onChange={(e) => set({ tags: e.target.value })} placeholder="app=checkout, tier=1" spellCheck={false} /></Field>
        <Field label="Maintenance window" hint="When restarts and heavy maintenance may happen"><Input value={d.maintenance_window} onChange={(e) => set({ maintenance_window: e.target.value })} placeholder="Sun 02:00-04:00 UTC" /></Field>
        <Field label="Description" className="sm:col-span-2"><Textarea rows={2} value={d.description} onChange={(e) => set({ description: e.target.value })} placeholder="What runs on it and what breaks when it is slow" /></Field>
      </div>
      <div>
        <div className="plate mb-1">Host profile</div>
        <p className="mb-2 text-[12.5px] text-ink-3">Used to size memory, WAL and parallelism. Managed services: use the instance size.</p>
        <div className="grid gap-3 sm:grid-cols-4">
          <Field label="Memory (GB)"><Input type="number" min={1} value={d.ram_gb} onChange={(e) => set({ ram_gb: e.target.value })} placeholder="64" /></Field>
          <Field label="vCPUs"><Input type="number" min={1} value={d.vcpus} onChange={(e) => set({ vcpus: e.target.value })} placeholder="16" /></Field>
          <Field label="Storage">
            <Select value={d.storage} onChange={(e) => set({ storage: e.target.value })}>
              <option value="ssd">SSD</option><option value="nvme">NVMe</option><option value="network">Network (EBS, managed disk)</option><option value="hdd">Spinning disk</option>
            </Select>
          </Field>
          <Field label="Workload">
            <Select value={d.workload} onChange={(e) => set({ workload: e.target.value })}>
              <option value="oltp">Transactions (OLTP)</option><option value="olap">Analytics (OLAP)</option><option value="mixed">Mixed</option>
            </Select>
          </Field>
        </div>
      </div>
      <div className="grid gap-3 sm:grid-cols-[1fr_200px]">
        <Field label="Azure resource ID (optional)" hint="Adds Azure Monitor platform metrics next to the SQL collector">
          <Input value={d.azure_resource_id} onChange={(e) => set({ azure_resource_id: e.target.value })} spellCheck={false} className="font-mono text-[12px]"
            placeholder="/subscriptions/…/resourceGroups/…/providers/Microsoft.DBforPostgreSQL/flexibleServers/…" />
        </Field>
        <Field label="Collect every">
          <Select value={d.collect_interval_s} onChange={(e) => set({ collect_interval_s: Number(e.target.value) })}>
            {[15, 30, 60, 120, 300].map((s) => <option key={s} value={s}>{s < 60 ? `${s} seconds` : `${s / 60} minute${s > 60 ? "s" : ""}`}</option>)}
          </Select>
        </Field>
      </div>
    </div>
  );
}
