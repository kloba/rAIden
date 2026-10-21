SET statement_timeout='5s';
SELECT
 coalesce((SELECT string_agg(schema_name||'.'||table_name,',' ORDER BY schema_name,table_name) FROM raiden.protected_tables),'') || '|' ||
 (SELECT count(*) FROM pg_event_trigger WHERE evtname='raiden_protect_tables' AND evtenabled='O' AND evtevent='sql_drop' AND evttags IS NULL AND evtfoid='raiden.block_protected_drop()'::regprocedure AND pg_get_userbyid(evtowner)='raiden') || '|' ||
 md5(pg_get_functiondef('raiden.block_protected_drop()'::regprocedure)) || '|' ||
 (SELECT count(*) FROM raiden.protected_tables p LEFT JOIN pg_namespace n ON n.nspname=p.schema_name LEFT JOIN pg_class c ON c.relnamespace=n.oid AND c.relname=p.table_name AND c.relkind IN ('r','p') WHERE c.oid IS NULL) || '|' ||
 (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p') AND n.nspname='public' AND c.relname !~ '^(tmp|temp|scratch|restore|restored|quarantine|pitr)_' AND NOT EXISTS(SELECT 1 FROM raiden.protected_tables p WHERE p.schema_name=n.nspname AND p.table_name=c.relname)) || '|' ||
 has_table_privilege('arena','raiden.protected_tables','INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER') || '|' ||
 has_schema_privilege('arena','raiden','CREATE') || '|' ||
 pg_get_userbyid((SELECT relowner FROM pg_class WHERE oid='raiden.protected_tables'::regclass)) || '|' ||
 pg_get_userbyid((SELECT proowner FROM pg_proc WHERE oid='raiden.block_protected_drop()'::regprocedure));
