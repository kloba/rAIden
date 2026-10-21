"""Azure Monitor platform metrics for Azure Database for PostgreSQL flexible servers (optional extra source).

The SQL collector cannot see the host (CPU, memory, IOPS, storage, network). For a flexible server those come from
Azure Monitor: GET {resourceId}/providers/Microsoft.Insights/metrics with an Entra ID token (DefaultAzureCredential:
managed identity, workload identity, environment or Azure CLI). The identity needs the Monitoring Reader role, whose
*/read also covers the server resource itself: its authentication, encryption and network settings feed the security
assessments that Fabric's Database Hub runs for PostgreSQL (see posture()).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..catalog import AZURE_METRICS, BY_ID

API_VERSION = "2024-02-01"
SERVER_API_VERSION = "2024-08-01"
ARM = "https://management.azure.com"
# metrics SQL cannot produce (or produces better itself): ask Azure only for these
WANTED = [
    "cpu_percent", "memory_percent", "storage_percent", "storage_used", "storage_free", "iops", "read_iops", "write_iops",
    "read_throughput", "write_throughput", "disk_queue_depth", "disk_iops_consumed_percentage", "disk_bandwidth_consumed_percentage",
    "network_bytes_ingress", "network_bytes_egress", "connections_failed", "connections_succeeded", "backup_storage_used",
    "cpu_credits_remaining", "tcp_connection_backlog", "client_connections_active", "client_connections_waiting",
    "server_connections_active", "server_connections_idle", "total_pooled_connections", "num_pools", "client_connections_total",
    "cpu_credits_consumed", "postmaster_process_cpu_usage_percent", "logical_replication_slot_sync_status",
]
TOTALS = {"connections_failed", "connections_succeeded", "network_bytes_ingress", "network_bytes_egress"}


def parse_metrics(payload: dict[str, Any]) -> dict[str, float]:
    """Latest non-empty point of every metric in an Azure Monitor metrics response, keyed by catalog id."""
    out: dict[str, float] = {}
    for item in payload.get("value", []):
        name = (item.get("name") or {}).get("value", "")
        metric_id = AZURE_METRICS.get(name)
        if not metric_id:
            continue
        for series in item.get("timeseries", []) or []:
            for point in reversed(series.get("data", []) or []):
                order = ("total", "maximum", "average") if metric_id in TOTALS else ("average", "maximum", "total")
                value = next((point[k] for k in order if point.get(k) is not None), None)
                if value is not None:
                    out[metric_id] = float(value)
                    break
            if metric_id in out:
                break
    return out


def posture(resource: dict[str, Any]) -> dict[str, Any]:
    """The security settings of a flexible server resource (GET Microsoft.DBforPostgreSQL/flexibleServers/{name})."""
    props = resource.get("properties") or {}
    auth = props.get("authConfig") or {}
    enc = props.get("dataEncryption") or {}
    net = props.get("network") or {}
    return {
        "entra_auth": str(auth.get("activeDirectoryAuth", "")).lower() == "enabled",
        "password_auth": str(auth.get("passwordAuth", "Enabled")).lower() == "enabled",
        "cmk": str(enc.get("type", "")).lower() == "azurekeyvault",
        "public_network": str(net.get("publicNetworkAccess", "")).lower() == "enabled",
        "sku": (resource.get("sku") or {}).get("name", ""), "location": resource.get("location", ""),
    }


class AzureMonitorSource:
    def __init__(self, resource_id: str, credential: Any = None) -> None:
        self.resource_id = resource_id.rstrip("/")
        self._credential = credential
        self._token: tuple[str, float] | None = None
        self.last_error = ""

    def _get_token(self) -> str:
        if self._token and self._token[1] - 120 > time.time():
            return self._token[0]
        if self._credential is None:
            from azure.identity import DefaultAzureCredential

            self._credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        tok = self._credential.get_token(f"{ARM}/.default")
        self._token = (tok.token, float(tok.expires_on))
        return tok.token

    async def fetch(self, minutes: int = 10) -> dict[str, float]:
        token = await asyncio.to_thread(self._get_token)
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        start = end - timedelta(minutes=minutes)
        names = [BY_ID[m].azure for m in WANTED if m in BY_ID and BY_ID[m].azure]
        out: dict[str, float] = {}
        async with httpx.AsyncClient(timeout=20) as client:
            for i in range(0, len(names), 20):  # the API accepts at most 20 metric names per call
                resp = await client.get(
                    f"{ARM}{self.resource_id}/providers/Microsoft.Insights/metrics",
                    params={
                        "api-version": API_VERSION, "metricnames": ",".join(names[i:i + 20]), "interval": "PT1M",
                        "timespan": f"{start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}", "aggregation": "Average,Maximum,Total",
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code >= 400:
                    self.last_error = f"Azure Monitor {resp.status_code}: {resp.text[:200]}"
                    raise RuntimeError(self.last_error)
                out.update(parse_metrics(resp.json()))
        self.last_error = ""
        return out

    async def posture(self) -> dict[str, Any]:
        """The server resource's authentication, encryption and network settings (see posture())."""
        token = await asyncio.to_thread(self._get_token)
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{ARM}{self.resource_id}", params={"api-version": SERVER_API_VERSION}, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code >= 400:
            raise RuntimeError(f"Azure Resource Manager {resp.status_code}: {resp.text[:200]}")
        return posture(resp.json())
