import { useQuery } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router";
import { Shell } from "./components/Shell";
import { Loading, ToastProvider } from "./components/ui";
import { api } from "./lib/api";
import { useMe } from "./lib/hooks";
import type { AuthStatus } from "./lib/types";
import { AIEndpointsPage } from "./pages/AIEndpoints";
import { ApprovalsPage } from "./pages/Approvals";
import { AuditPage } from "./pages/Audit";
import { LoginPage, SetupPage } from "./pages/Auth";
import { AutonomyPage } from "./pages/Autonomy";
import { ChatPage } from "./pages/Chat";
import { DatabasesPage } from "./pages/Databases";
import { EstatePage } from "./pages/Estate";
import { IncidentPage } from "./pages/IncidentPage";
import { IncidentsPage } from "./pages/Incidents";
import { MonitorsPage } from "./pages/Monitors";
import { OnboardPage } from "./pages/Onboard";
import { OverviewPage } from "./pages/Overview";
import { PerformancePage } from "./pages/Performance";
import { RoutinesPage } from "./pages/Routines";
import { ServerPage } from "./pages/ServerPage";
import { SettingsPage } from "./pages/Settings";

function Guarded() {
  const me = useMe();
  const status = useQuery({ queryKey: ["auth-status"], queryFn: () => api.get<AuthStatus>("/auth/status"), staleTime: 60_000 });
  const loc = useLocation();
  if (me.isLoading || status.isLoading) return <Loading label="Starting the console…" />;
  if (status.data?.needs_setup) return <Navigate to="/setup" replace />;
  if (!me.data) return <Navigate to="/login" replace state={{ from: loc.pathname }} />;
  return <Shell demo={!!status.data?.demo} />;
}

export function App() {
  return (
    <ToastProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/setup" element={<SetupPage />} />
          <Route element={<Guarded />}>
            <Route path="/" element={<OverviewPage />} />
            <Route path="/estate" element={<EstatePage />} />
            <Route path="/servers/:id" element={<ServerPage />} />
            <Route path="/performance" element={<PerformancePage />} />
            <Route path="/incidents" element={<IncidentsPage />} />
            <Route path="/incidents/:id" element={<IncidentPage />} />
            <Route path="/approvals" element={<ApprovalsPage />} />
            <Route path="/chat" element={<ChatPage />} />
            <Route path="/chat/:id" element={<ChatPage />} />
            <Route path="/routines" element={<RoutinesPage />} />
            <Route path="/monitors" element={<MonitorsPage />} />
            <Route path="/databases" element={<DatabasesPage />} />
            <Route path="/databases/new" element={<OnboardPage />} />
            <Route path="/ai" element={<AIEndpointsPage />} />
            <Route path="/autonomy" element={<AutonomyPage />} />
            <Route path="/audit" element={<AuditPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </ToastProvider>
  );
}
