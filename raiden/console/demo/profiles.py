"""The demo fleet: seven PostgreSQL servers on six platforms, all simulated (nothing real is contacted)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SUB = "00000000-0000-4000-8000-000000000000"


@dataclass(frozen=True)
class Profile:
    id: str
    environment: str
    platform: str
    version: str
    version_num: int
    host: str
    region: str
    sku: str
    cores: int
    mem_gb: int
    disk_gb: int
    max_connections: int
    base_tps: float
    databases: tuple[str, ...]
    sources: tuple[str, ...]  # sql | host | azure
    owner: str
    description: str
    tags: dict[str, str] = field(default_factory=dict)
    replica_of: str | None = None
    azure_resource_id: str | None = None
    storage_start: float = 50.0  # storage % 30 days ago
    storage_end: float = 58.0
    app_role: str = "app"
    maintenance_window: str = "Sun 02:00-04:00 UTC"
    tz_shift_h: float = 0.0


PROFILES: list[Profile] = [
    Profile(
        "orders-prod", "production", "azure_flexible", "PostgreSQL 17.6", 170006, "orders-prod.postgres.database.azure.com",
        "Sweden Central", "General Purpose D8ds_v5 · 8 vCores · 32 GiB · 1 TiB P30", 8, 32, 1024, 859, 1450.0,
        ("orders", "postgres"), ("sql", "azure"), "team-commerce", "Order intake for the web shop and the mobile apps.",
        {"team": "commerce", "tier": "1"}, azure_resource_id=f"/subscriptions/{SUB}/resourceGroups/rg-commerce-prod/providers/"
        "Microsoft.DBforPostgreSQL/flexibleServers/orders-prod", storage_start=54, storage_end=61, app_role="orders_app",
    ),
    Profile(
        "payments-prod", "production", "self_managed", "PostgreSQL 18.1", 180001, "pg-pay-01.fra1.internal",
        "Frankfurt FRA1 (on-prem)", "2× EPYC 9354 · 256 GiB · NVMe RAID 10", 64, 256, 3840, 600, 2650.0,
        ("payments", "ledger", "postgres"), ("sql", "host"), "team-payments",
        "Card payments and the double-entry ledger. Bare metal, rAIden host agent installed.",
        {"team": "payments", "pci": "in-scope", "tier": "0"}, storage_start=41, storage_end=47, app_role="payments_app",
    ),
    Profile(
        "payments-replica", "production", "self_managed", "PostgreSQL 18.1", 180001, "pg-pay-02.fra1.internal",
        "Frankfurt FRA1 (on-prem)", "2× EPYC 9354 · 256 GiB · NVMe RAID 10", 64, 256, 3840, 600, 420.0,
        ("payments", "ledger", "postgres"), ("sql", "host"), "team-payments",
        "Hot standby of payments-prod; serves BI and finance reports.", {"team": "payments", "pci": "in-scope", "role": "replica"},
        replica_of="payments-prod", storage_start=41, storage_end=47, app_role="bi_reader",
    ),
    Profile(
        "inventory-prod", "production", "aws_rds", "PostgreSQL 16.10", 160010, "inventory-prod.c8x2kq3z.eu-west-1.rds.amazonaws.com",
        "AWS eu-west-1", "db.r7g.2xlarge · 8 vCPU · 64 GiB · gp3", 8, 64, 2000, 1600, 640.0,
        ("inventory", "postgres"), ("sql",), "team-supply", "Stock levels and warehouse movements, synced from the ERP over 2PC.",
        {"team": "supply", "tier": "1"}, storage_start=63, storage_end=69, app_role="inventory_app",
    ),
    Profile(
        "analytics-staging", "staging", "kubernetes", "PostgreSQL 17.6", 170006, "analytics-rw.data.svc.cluster.local",
        "AKS westeurope · CloudNativePG", "4 vCPU · 16 GiB · premium SSD v2", 4, 16, 512, 200, 95.0,
        ("analytics", "postgres"), ("sql",), "team-data", "Staging copy of the analytics mart; reporting jobs run here first.",
        {"team": "data"}, storage_start=38, storage_end=44, app_role="etl",
    ),
    Profile(
        "crm-prod", "production", "gcp_cloudsql", "PostgreSQL 15.14", 150014, "10.20.0.5 · crm-prod (europe-west3)",
        "GCP europe-west3", "db-custom-4-16384 · HA", 4, 16, 500, 400, 310.0,
        ("crm", "postgres"), ("sql",), "team-sales", "Customer accounts and the sales pipeline.", {"team": "sales", "tier": "2"},
        storage_start=29, storage_end=33, app_role="crm_app",
    ),
    Profile(
        "ledger-dev", "development", "vm", "PostgreSQL 18.1", 180001, "ledger-dev.lab.internal",
        "Azure VM · Sweden Central", "Standard_D4s_v5 · 4 vCPU · 16 GiB · 256 GiB", 4, 16, 256, 100, 38.0,
        ("ledger", "postgres"), ("sql", "host"), "team-platform",
        "Developer sandbox for the ledger service. Also where pg_crash lives.", {"team": "platform", "chaos": "pg_crash"},
        storage_start=46, storage_end=52, app_role="ledger_dev", maintenance_window="any time",
    ),
]

# Not in the estate: the server a demo user can onboard (Databases → Add a database). Upgraded from PostgreSQL 12 with
# pg_upgrade and never tuned, so the assessment has something to say in every area.
CANDIDATES: list[Profile] = [
    Profile(
        "billing-prod", "production", "self_managed", "PostgreSQL 16.10", 160010, "pg-billing-01.dc2.internal",
        "Prague DC2 (on-prem)", "Dell R660 · 32 cores · 128 GiB · SSD RAID 10", 32, 128, 1920, 300, 520.0,
        ("billing", "postgres"), ("sql",), "team-billing", "Invoices and subscriptions. Upgraded from PostgreSQL 12 with pg_upgrade; never tuned.",
        {"team": "billing", "tier": "1"}, storage_start=33, storage_end=36, app_role="billing_app",
    ),
]
UNTUNED = {p.id for p in CANDIDATES}

BY_ID: dict[str, Profile] = {p.id: p for p in PROFILES + CANDIDATES}


def candidate_for(host: str) -> Profile | None:
    """The demo onboarding candidate behind a host name, if any."""
    return next((p for p in CANDIDATES if p.host == host.strip().lower()), None)


# What each server's application does: (query, mean ms, share of calls, rows per call)
QUERIES: dict[str, list[tuple[str, float, float, int]]] = {
    "orders-prod": [
        ("INSERT INTO orders.order_lines (order_id, sku, qty, price) VALUES ($1, $2, $3, $4)", 0.42, 0.34, 1),
        ("SELECT o.id, o.status, o.total FROM orders.orders o WHERE o.customer_id = $1 ORDER BY o.created_at DESC LIMIT $2", 1.8, 0.22, 20),
        ("UPDATE orders.orders SET status = $1, updated_at = now() WHERE id = $2", 0.61, 0.18, 1),
        ("SELECT count(*) FROM orders.orders WHERE status = $1 AND created_at > now() - interval '15 minutes'", 38.0, 0.01, 1),
        ("INSERT INTO orders.orders (customer_id, channel, total) VALUES ($1, $2, $3) RETURNING id", 0.55, 0.15, 1),
        ("SELECT * FROM orders.payments_outbox WHERE sent_at IS NULL ORDER BY id LIMIT $1 FOR UPDATE SKIP LOCKED", 2.4, 0.10, 50),
    ],
    "payments-prod": [
        ("INSERT INTO ledger.entries (account_id, amount, currency, ref) VALUES ($1, $2, $3, $4)", 0.31, 0.38, 1),
        ("SELECT balance FROM ledger.accounts WHERE id = $1 FOR UPDATE", 0.22, 0.25, 1),
        ("UPDATE ledger.accounts SET balance = balance + $1 WHERE id = $2", 0.28, 0.25, 1),
        ("SELECT t.* FROM payments.transactions t WHERE t.merchant_id = $1 AND t.created_at >= $2 ORDER BY t.created_at", 4.1, 0.08, 120),
        ("INSERT INTO payments.authorizations (card_token, amount, merchant_id) VALUES ($1, $2, $3)", 0.36, 0.04, 1),
    ],
    "payments-replica": [
        ("SELECT merchant_id, date_trunc('day', created_at), sum(amount) FROM payments.transactions WHERE created_at >= $1 GROUP BY 1, 2", 2400.0, 0.05, 900),
        ("SELECT t.* FROM payments.transactions t WHERE t.merchant_id = $1 AND t.created_at >= $2 ORDER BY t.created_at", 4.6, 0.75, 120),
        ("SELECT a.id, a.balance FROM ledger.accounts a WHERE a.owner_id = $1", 0.4, 0.20, 3),
    ],
    "inventory-prod": [
        ("UPDATE inventory.stock SET on_hand = on_hand - $1 WHERE sku = $2 AND warehouse_id = $3", 0.7, 0.40, 1),
        ("INSERT INTO inventory.stock_movements (sku, warehouse_id, delta, reason) VALUES ($1, $2, $3, $4)", 0.5, 0.35, 1),
        ("SELECT sku, sum(on_hand) FROM inventory.stock WHERE sku = ANY($1) GROUP BY sku", 3.2, 0.20, 40),
        ("PREPARE TRANSACTION $1", 1.1, 0.05, 0),
    ],
    "analytics-staging": [
        ("SELECT region, product_line, sum(revenue) FROM mart.fact_sales WHERE day BETWEEN $1 AND $2 GROUP BY ROLLUP (region, product_line)", 8200.0, 0.10, 120),
        ("INSERT INTO mart.fact_sales SELECT * FROM staging.sales_batch WHERE batch_id = $1", 15400.0, 0.02, 250000),
        ("SELECT * FROM mart.dim_customer WHERE customer_key = $1", 0.9, 0.88, 1),
    ],
    "crm-prod": [
        ("SELECT a.* FROM crm.accounts a WHERE a.owner_id = $1 ORDER BY a.updated_at DESC LIMIT 50", 2.2, 0.45, 50),
        ("UPDATE crm.opportunities SET stage = $1, amount = $2 WHERE id = $3", 0.8, 0.30, 1),
        ("SELECT o.* FROM crm.opportunities o JOIN crm.accounts a ON a.id = o.account_id WHERE a.region = $1", 41.0, 0.05, 400),
        ("INSERT INTO crm.activities (account_id, kind, note) VALUES ($1, $2, $3)", 0.6, 0.20, 1),
    ],
    "ledger-dev": [
        ("INSERT INTO public.entries (account, amount, note) VALUES ($1, $2, $3)", 0.5, 0.7, 1),
        ("SELECT sum(amount) FROM public.entries WHERE account = $1", 12.0, 0.3, 1),
    ],
    "billing-prod": [
        ("INSERT INTO billing.invoice_lines (invoice_id, product_id, qty, amount) VALUES ($1, $2, $3, $4)", 0.6, 0.36, 1),
        ("SELECT i.* FROM billing.invoices i WHERE i.customer_id = $1 ORDER BY i.issued_at DESC LIMIT 20", 3.4, 0.30, 20),
        ("UPDATE billing.subscriptions SET renewed_at = now(), period_end = $1 WHERE id = $2", 0.9, 0.18, 1),
        ("INSERT INTO billing.payment_attempts (invoice_id, provider, status) VALUES ($1, $2, $3)", 0.5, 0.14, 1),
        ("SELECT sum(amount) FROM billing.invoice_lines l JOIN billing.invoices i ON i.id = l.invoice_id WHERE i.issued_at >= $1", 2900.0, 0.02, 1),
    ],
}

TABLES: dict[str, list[tuple[str, int, float]]] = {  # (table, live rows, typical dead %)
    "orders-prod": [("orders.order_lines", 412_000_000, 3.1), ("orders.orders", 96_000_000, 4.8), ("orders.payments_outbox", 1_200_000, 22.0),
                    ("orders.customers", 8_400_000, 1.2), ("orders.order_events", 640_000_000, 0.4)],
    "payments-prod": [("ledger.entries", 2_100_000_000, 0.8), ("payments.transactions", 880_000_000, 1.9), ("ledger.accounts", 14_000_000, 9.5),
                      ("payments.authorizations", 310_000_000, 2.2)],
    "payments-replica": [("ledger.entries", 2_100_000_000, 0.8), ("payments.transactions", 880_000_000, 1.9), ("ledger.accounts", 14_000_000, 9.5)],
    "inventory-prod": [("inventory.stock_movements", 1_450_000_000, 6.0), ("inventory.stock", 22_000_000, 14.0), ("inventory.warehouses", 140, 0.0),
                       ("inventory.sync_queue", 380_000, 31.0)],
    "analytics-staging": [("mart.fact_sales", 940_000_000, 0.2), ("mart.dim_customer", 21_000_000, 0.7), ("staging.sales_batch", 2_600_000, 48.0)],
    "crm-prod": [("crm.activities", 260_000_000, 2.7), ("crm.accounts", 3_100_000, 5.5), ("crm.opportunities", 9_800_000, 12.0)],
    "ledger-dev": [("public.entries", 3_200_000, 4.0), ("public.accounts", 1_000, 2.0)],
    "billing-prod": [("billing.audit_log", 910_000_000, 0.1), ("billing.invoice_lines", 640_000_000, 2.4), ("billing.payment_attempts", 310_000_000, 1.1),
                     ("billing.invoices", 120_000_000, 6.5), ("billing.subscriptions", 2_400_000, 26.0)],
}

ROLES: dict[str, list[str]] = {
    "orders-prod": ["orders_app", "orders_worker", "analyst", "migrator", "azure_superuser"],
    "payments-prod": ["payments_app", "ledger_writer", "bi_reader", "postgres"],
    "payments-replica": ["bi_reader", "finance_report", "payments_app"],
    "inventory-prod": ["inventory_app", "erp_sync", "rdsadmin"],
    "analytics-staging": ["etl", "reporting", "analyst"],
    "crm-prod": ["crm_app", "crm_sync", "cloudsqladmin"],
    "ledger-dev": ["ledger_dev", "raiden", "postgres"],
    "billing-prod": ["billing_app", "billing_batch", "dba_jan", "postgres"],
}


def settings_for(p: Profile) -> list[dict[str, Any]]:
    """A realistic slice of pg_settings for the profile (name, setting, unit, source, pending_restart, context)."""
    if p.id in UNTUNED:
        return _untuned(p)
    mem_mb = p.mem_gb * 1024
    rows = [
        ("max_connections", str(p.max_connections), None, "configuration file", False, "postmaster"),
        ("shared_buffers", str(mem_mb // 4 * 128), "8kB", "configuration file", False, "postmaster"),
        ("effective_cache_size", str(mem_mb * 3 // 4 * 128), "8kB", "configuration file", False, "user"),
        ("work_mem", "16384" if p.mem_gb >= 64 else "8192", "kB", "configuration file", False, "user"),
        ("maintenance_work_mem", "2097152" if p.mem_gb >= 64 else "524288", "kB", "configuration file", False, "user"),
        ("max_wal_size", "16384" if p.mem_gb >= 64 else "4096", "MB", "configuration file", False, "sighup"),
        ("checkpoint_timeout", "900", "s", "configuration file", False, "sighup"),
        ("checkpoint_completion_target", "0.9", None, "default", False, "sighup"),
        ("random_page_cost", "1.1", None, "configuration file", False, "user"),
        ("effective_io_concurrency", "200", None, "configuration file", False, "user"),
        ("autovacuum_max_workers", "6" if p.cores >= 8 else "3", None, "configuration file", False, "postmaster"),
        ("autovacuum_vacuum_scale_factor", "0.05", None, "configuration file", False, "sighup"),
        ("autovacuum_vacuum_cost_limit", "2000", None, "configuration file", False, "sighup"),
        ("autovacuum_freeze_max_age", "200000000", None, "default", False, "postmaster"),
        ("idle_in_transaction_session_timeout", "0", "ms", "default", False, "user"),
        ("lock_timeout", "0", "ms", "default", False, "user"),
        ("statement_timeout", "0", "ms", "default", False, "user"),
        ("log_lock_waits", "on", None, "configuration file", False, "superuser"),
        ("log_min_duration_statement", "1000", "ms", "configuration file", False, "superuser"),
        ("track_io_timing", "on", None, "configuration file", False, "superuser"),
        ("wal_level", "logical" if p.id in ("ledger-dev", "orders-prod") else "replica", None, "configuration file", False, "postmaster"),
        ("max_slot_wal_keep_size", "-1", "MB", "default", False, "sighup"),
        ("hot_standby_feedback", "off", None, "default", False, "sighup"),
        ("max_standby_streaming_delay", "-1" if p.id == "payments-replica" else "30000", "ms", "configuration file" if p.id == "payments-replica" else "default", False, "sighup"),
        ("shared_preload_libraries", "pg_stat_statements" + (",auto_explain" if p.id == "payments-prod" else ""), None, "configuration file", False, "postmaster"),
        ("password_encryption", "md5" if p.id == "crm-prod" else "scram-sha-256", None, "configuration file", False, "user"),
        ("ssl", "on", None, "configuration file", False, "sighup"),
        ("log_connections", "off" if p.id in ("crm-prod", "analytics-staging") else "on", None, "configuration file", False, "superuser-backend"),
        ("data_checksums", "on" if p.platform != "aws_rds" else "off", None, "default", False, "internal"),
        ("archive_mode", "on" if p.platform in ("self_managed", "vm") else "on", None, "configuration file", False, "postmaster"),
        ("jit", "off" if p.id == "orders-prod" else "on", None, "configuration file", False, "user"),
    ]
    if p.id == "ledger-dev":  # someone edited the file and reloaded: it waits for the next restart
        rows[1] = ("shared_buffers", "131072", "8kB", "configuration file", True, "postmaster")
        rows[24] = ("shared_preload_libraries", "pg_stat_statements", None, "configuration file", True, "postmaster")
    return [dict(zip(("name", "setting", "unit", "source", "pending_restart", "context"), r)) for r in rows]


def _untuned(p: Profile) -> list[dict[str, Any]]:
    """What initdb and the package left behind: defaults sized for a laptop, on a 32-core, 128 GiB server."""
    rows = [
        ("max_connections", str(p.max_connections), None, "configuration file", False, "postmaster"),
        ("shared_buffers", "16384", "8kB", "configuration file", False, "postmaster"),
        ("effective_cache_size", "524288", "8kB", "default", False, "user"),
        ("work_mem", "4096", "kB", "default", False, "user"),
        ("maintenance_work_mem", "65536", "kB", "default", False, "user"),
        ("max_wal_size", "1024", "MB", "configuration file", False, "sighup"),
        ("min_wal_size", "80", "MB", "configuration file", False, "sighup"),
        ("checkpoint_timeout", "300", "s", "default", False, "sighup"),
        ("checkpoint_completion_target", "0.9", None, "default", False, "sighup"),
        ("random_page_cost", "4", None, "default", False, "user"),
        ("effective_io_concurrency", "1", None, "default", False, "user"),
        ("max_worker_processes", "8", None, "default", False, "postmaster"),
        ("max_parallel_workers", "8", None, "default", False, "user"),
        ("max_parallel_workers_per_gather", "2", None, "default", False, "user"),
        ("autovacuum_max_workers", "3", None, "default", False, "postmaster"),
        ("autovacuum_vacuum_scale_factor", "0.2", None, "default", False, "sighup"),
        ("autovacuum_vacuum_cost_limit", "-1", None, "default", False, "sighup"),
        ("autovacuum_freeze_max_age", "200000000", None, "default", False, "postmaster"),
        ("idle_in_transaction_session_timeout", "0", "ms", "default", False, "user"),
        ("lock_timeout", "0", "ms", "default", False, "user"),
        ("statement_timeout", "0", "ms", "default", False, "user"),
        ("log_lock_waits", "off", None, "default", False, "superuser"),
        ("log_min_duration_statement", "-1", "ms", "default", False, "superuser"),
        ("log_autovacuum_min_duration", "-1", "ms", "configuration file", False, "sighup"),
        ("log_checkpoints", "on", None, "default", False, "sighup"),
        ("log_connections", "off", None, "default", False, "superuser-backend"),
        ("track_io_timing", "off", None, "default", False, "superuser"),
        ("wal_level", "replica", None, "default", False, "postmaster"),
        ("wal_compression", "off", None, "default", False, "superuser"),
        ("archive_mode", "off", None, "default", False, "postmaster"),
        ("max_slot_wal_keep_size", "-1", "MB", "default", False, "sighup"),
        ("hot_standby_feedback", "off", None, "default", False, "sighup"),
        ("max_standby_streaming_delay", "30000", "ms", "default", False, "sighup"),
        ("shared_preload_libraries", "", None, "default", False, "postmaster"),
        ("password_encryption", "md5", None, "configuration file", False, "user"),
        ("ssl", "on", None, "configuration file", False, "sighup"),
        ("data_checksums", "off", None, "default", False, "internal"),
        ("huge_pages", "try", None, "default", False, "postmaster"),
        ("jit", "on", None, "default", False, "user"),
    ]
    return [dict(zip(("name", "setting", "unit", "source", "pending_restart", "context"), r)) for r in rows]
