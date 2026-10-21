-- find_bad_rows.sql - list public.entries rows whose payload cannot be fully read (TOAST or heap corruption).
--
-- Usage (read-only on the table; creates only a TEMP table; works on production and on a PITR clone):
--   psql -X -q -d ledger -v ON_ERROR_STOP=1 \
--        -f /opt/raiden/.claude/skills/toast-corruption/scripts/find_bad_rows.sql > "$RAIDEN_INCIDENT_DIR/bad_rows.csv"
--   clone:  psql -X -q -h <socket dir> -p 5433 -d ledger -v ON_ERROR_STOP=1 -f .../find_bad_rows.sql
-- Optional: -v blocks=200   heap blocks checked per probe (smaller = finer fallback, slower).
--
-- stdout: CSV with header "id,ctid,err"; only the header line means no damaged row was found.
-- stderr: progress NOTICEs and a final "bad rows found: N".
-- Method: every payload is fully detoasted with md5() inside an exception block. Ranges of heap blocks are
-- probed first (TID range scan, cheap); a failing range is re-checked block by block and row by row.
-- A row with empty id means the whole heap block could not be read (keys must come from a clone).
\set QUIET on
\set ON_ERROR_STOP 1
\if :{?blocks}
\else
\set blocks 200
\endif
SET statement_timeout = 0;
SET lock_timeout = '5s';
SELECT set_config('raiden.blocks', :'blocks', false) AS raiden_blocks \gset
CREATE TEMP TABLE raiden_bad_rows (id bigint, row_ctid tid, err text);

DO $$
DECLARE
  step  int    := greatest(current_setting('raiden.blocks')::int, 1);
  nblk  bigint := pg_relation_size('public.entries') / current_setting('block_size')::int;
  b     bigint := 0;
  n     bigint;
  r     record;
BEGIN
  RAISE NOTICE 'scanning public.entries: % heap blocks, % blocks per probe', nblk, step;
  WHILE b < nblk LOOP
    BEGIN
      SELECT count(md5(l.payload)) INTO n FROM public.entries l
       WHERE l.ctid >= format('(%s,0)', b)::tid AND l.ctid < format('(%s,0)', b + step)::tid;
    EXCEPTION WHEN OTHERS THEN
      RAISE NOTICE 'error in blocks %..%: % - checking row by row', b, least(b + step, nblk) - 1, SQLERRM;
      FOR blk IN b .. least(b + step, nblk) - 1 LOOP
        BEGIN
          FOR r IN SELECT l.ctid AS t, l.id FROM public.entries l
                    WHERE l.ctid >= format('(%s,0)', blk)::tid AND l.ctid < format('(%s,0)', blk + 1)::tid
          LOOP
            BEGIN
              PERFORM md5(l2.payload) FROM public.entries l2 WHERE l2.ctid = r.t;
            EXCEPTION WHEN OTHERS THEN
              INSERT INTO raiden_bad_rows VALUES (r.id, r.t, SQLSTATE || ' ' || SQLERRM);
            END;
          END LOOP;
        EXCEPTION WHEN OTHERS THEN
          INSERT INTO raiden_bad_rows
          VALUES (NULL, format('(%s,0)', blk)::tid, 'HEAP BLOCK UNREADABLE: ' || SQLSTATE || ' ' || SQLERRM);
        END;
      END LOOP;
    END;
    b := b + step;
  END LOOP;
END
$$;

SELECT count(*) AS raiden_bad_count FROM raiden_bad_rows \gset
\warn 'bad rows found: ' :raiden_bad_count
\pset format csv
\pset footer off
SELECT id, row_ctid AS ctid, err FROM raiden_bad_rows ORDER BY id NULLS FIRST, row_ctid;
