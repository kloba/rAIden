import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { api } from "./api";
import type {
  Approval, Assessment, AuditEntry, Catalog, Conversation, Endpoint, EstateRow, FleetSeries, IncidentCard, IncidentDetail, KnowledgePage, Monitor,
  Overview, Policy, Routine, RoutineRun, Series, Server, ServerDetail, SettingsPayload, Table, Usage, User,
} from "./types";

export const serverColor = (slot: number | undefined) => `var(--s${((slot ?? 0) % 8) + 1})`;

export const useMe = () => useQuery({ queryKey: ["me"], queryFn: () => api.get<User>("/auth/me"), retry: false, staleTime: 60_000 });
export const useCatalog = () => useQuery({ queryKey: ["catalog"], queryFn: () => api.get<Catalog>("/metrics/catalog"), staleTime: Infinity });

export function useMetricDefs() {
  const { data } = useCatalog();
  const map = new Map((data?.metrics ?? []).map((m) => [m.id, m]));
  return (id: string) => map.get(id.split("[")[0]!);
}

export const useOverview = (range: string) =>
  useQuery({ queryKey: ["overview", range], queryFn: () => api.get<Overview>(`/overview?range=${range}`), refetchInterval: 30_000, placeholderData: keepPreviousData });
export const useEstate = () => useQuery({ queryKey: ["estate"], queryFn: () => api.get<EstateRow[]>("/estate"), refetchInterval: 30_000, placeholderData: keepPreviousData });
export const useServers = () => useQuery({ queryKey: ["servers"], queryFn: () => api.get<Server[]>("/servers"), staleTime: 15_000 });
export const useServer = (id: string | undefined) =>
  useQuery({ queryKey: ["server", id], queryFn: () => api.get<ServerDetail>(`/servers/${id}`), enabled: !!id, refetchInterval: 20_000, placeholderData: keepPreviousData });

export const useServerMetrics = (id: string | undefined, keys: string[], range: string, agg = "avg") =>
  useQuery({
    queryKey: ["metrics", id, keys.join(","), range, agg],
    queryFn: () => api.get<Series>(`/servers/${id}/metrics?keys=${encodeURIComponent(keys.join(","))}&range=${range}&agg=${agg}`),
    enabled: !!id && keys.length > 0,
    refetchInterval: range === "15m" || range === "1h" ? 15_000 : 60_000,
    placeholderData: keepPreviousData,
  });

export const useFleetMetric = (metric: string, range: string, servers: string[] = []) =>
  useQuery({
    queryKey: ["fleet", metric, range, servers.join(",")],
    queryFn: () => api.get<FleetSeries>(`/metrics/fleet?metric=${encodeURIComponent(metric)}&range=${range}&servers=${servers.join(",")}`),
    refetchInterval: 30_000,
    placeholderData: keepPreviousData,
  });

export const useLive = (id: string | undefined, kind: string, params = "", enabled = true) =>
  useQuery({
    queryKey: ["live", id, kind, params],
    queryFn: () => api.get<Table>(`/servers/${id}/live/${kind}${params ? `?${params}` : ""}`),
    enabled: !!id && enabled,
    refetchInterval: 10_000,
    placeholderData: keepPreviousData,
  });

export const useIncidents = (status = "board") =>
  useQuery({ queryKey: ["incidents", status], queryFn: () => api.get<IncidentCard[]>(`/incidents?status=${status}`), refetchInterval: 20_000, placeholderData: keepPreviousData });
export const useIncident = (id: string | undefined) =>
  useQuery({ queryKey: ["incident", id], queryFn: () => api.get<IncidentDetail>(`/incidents/${id}`), enabled: !!id, refetchInterval: 15_000, placeholderData: keepPreviousData });
export const useApprovals = (status = "pending") =>
  useQuery({ queryKey: ["approvals", status], queryFn: () => api.get<Approval[]>(`/approvals?status=${status}`), refetchInterval: 20_000, placeholderData: keepPreviousData });
export const usePolicy = () => useQuery({ queryKey: ["autonomy"], queryFn: () => api.get<Policy>("/autonomy") });
export const useMonitors = () => useQuery({ queryKey: ["monitors"], queryFn: () => api.get<Monitor[]>("/monitors"), refetchInterval: 30_000 });
export const useRoutines = () => useQuery({ queryKey: ["routines"], queryFn: () => api.get<Routine[]>("/routines"), refetchInterval: 30_000 });
export const useKnowledge = () =>
  useQuery({ queryKey: ["knowledge"], queryFn: () => api.get<KnowledgePage>("/knowledge"), refetchInterval: (q) => (q.state.data?.dreaming ? 2000 : 60_000) });
export const useRun = (id: number | null) =>
  useQuery({ queryKey: ["run", id], queryFn: () => api.get<RoutineRun>(`/runs/${id}`), enabled: id != null, refetchInterval: (q) => (q.state.data?.status === "running" ? 1500 : false) });
export const useEndpoints = () => useQuery({ queryKey: ["endpoints"], queryFn: () => api.get<Endpoint[]>("/ai/endpoints"), refetchInterval: 60_000 });
export const useUsage = (days: number) => useQuery({ queryKey: ["usage", days], queryFn: () => api.get<Usage>(`/ai/usage?days=${days}`), refetchInterval: 60_000 });
export const useRouting = () =>
  useQuery({ queryKey: ["routing"], queryFn: () => api.get<{ purpose: string; endpoints: { id: number; name: string; model: string; provider: string }[]; skipped: string[] }[]>("/ai/routing") });
export const useAudit = (q: string) => useQuery({ queryKey: ["audit", q], queryFn: () => api.get<AuditEntry[]>(`/audit?${q}`), placeholderData: keepPreviousData });
export const useSettings = () => useQuery({ queryKey: ["settings"], queryFn: () => api.get<SettingsPayload>("/settings") });
export const useUsers = () => useQuery({ queryKey: ["users"], queryFn: () => api.get<User[]>("/users") });
export const useConversations = () => useQuery({ queryKey: ["conversations"], queryFn: () => api.get<Conversation[]>("/chat/conversations") });
export const useConversation = (id: string | undefined) =>
  useQuery({
    queryKey: ["conversation", id],
    queryFn: () => api.get<Conversation>(`/chat/conversations/${id}`),
    enabled: !!id,
    refetchInterval: (q) => (q.state.data?.busy ? 2000 : false), // rAIden continues after a decision in the background
  });
export const useAssessments = (server: string | undefined) =>
  useQuery({ queryKey: ["assessments", server], queryFn: () => api.get<Assessment[]>(`/servers/${server}/assessments`), enabled: !!server });
export const useAssessment = (id: number | null | undefined) =>
  useQuery({
    queryKey: ["assessment", id],
    queryFn: () => api.get<Assessment>(`/assessments/${id}`),
    enabled: id != null,
    refetchInterval: (q) => (q.state.data?.status === "running" ? 1500 : false),
  });

export function useDecide() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, approve, comment }: { id: number; approve: boolean; comment?: string }) => api.post(`/approvals/${id}/decide`, { approve, comment: comment ?? "" }),
    onSuccess: () => {
      for (const k of ["approvals", "incidents", "incident", "overview", "conversation"]) qc.invalidateQueries({ queryKey: [k] });
    },
  });
}

/** One EventSource for the whole app: background work tells the browser what to refetch.
 *  The stream is closed while the tab is hidden or the page sits in the back/forward cache: over HTTP/1.1 a browser keeps
 *  at most six connections to the console, and streams held by background tabs and cached pages would use them all up. */
export function useLiveEvents(enabled: boolean) {
  const qc = useQueryClient();
  const pending = useRef(new Set<string>());
  const timer = useRef<number | null>(null);
  useEffect(() => {
    if (!enabled) return;
    let es: EventSource | null = null;
    const flush = () => {
      timer.current = null;
      for (const key of pending.current) qc.invalidateQueries({ queryKey: [key] });
      pending.current.clear();
    };
    const mark = (...keys: string[]) => {
      keys.forEach((k) => pending.current.add(k));
      if (timer.current == null) timer.current = window.setTimeout(flush, 600);
    };
    const open = () => {
      if (es) return;
      es = new EventSource("/api/v1/events", { withCredentials: true });
      es.addEventListener("incident", () => mark("incidents", "incident", "overview", "approvals", "estate", "server"));
      es.addEventListener("approval", () => mark("approvals", "overview", "incidents", "incident", "conversation"));
      es.addEventListener("estate", () => mark("estate", "overview", "server"));
      es.addEventListener("routine", () => mark("routines", "run", "knowledge"));
      es.addEventListener("knowledge", () => mark("knowledge", "incident", "approvals"));
      es.addEventListener("assessment", () => mark("assessments", "assessment", "server", "estate"));
      es.addEventListener("chat", () => mark("conversation", "conversations"));
      es.addEventListener("autonomy", () => mark("autonomy", "estate", "incident", "knowledge"));
    };
    const close = () => {
      es?.close();
      es = null;
    };
    // back from a hidden tab or the back/forward cache: catch up on what happened meanwhile, then listen again
    const resume = () => {
      if (es) return;
      open();
      qc.invalidateQueries();
    };
    const onVisibility = () => (document.visibilityState === "hidden" ? close() : resume());
    const onPageShow = (e: PageTransitionEvent) => { if (e.persisted) resume(); };
    if (document.visibilityState !== "hidden") open();
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("pagehide", close);
    window.addEventListener("pageshow", onPageShow);
    return () => {
      close();
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("pagehide", close);
      window.removeEventListener("pageshow", onPageShow);
      if (timer.current != null) window.clearTimeout(timer.current);
    };
  }, [enabled, qc]);
}
