#!/usr/bin/env python3
"""Read-only bounded production lock wait history; no automatic cancellation."""
import datetime as dt
import glob
import json
import sys
import time

SQL = 'LOCK TABLE PUBLIC.ENTRIES IN ACCESS EXCLUSIVE MODE'

def norm(value):
    return ' '.join(value.strip().rstrip(';').upper().split())

def stamp(row):
    return dt.datetime.strptime(row['timestamp'], '%Y-%m-%d %H:%M:%S.%f UTC').replace(tzinfo=dt.timezone.utc).timestamp()

def classify(rows, now):
    waits, ends = [], []
    for r in rows:
        if r.get('dbname') != 'ledger' or r.get('user') != 'migrator' or norm(r.get('statement', '')) != SQL:
            continue
        try:
            t = stamp(r)
        except (KeyError, ValueError):
            continue
        if not now - 600 <= t <= now or not r.get('session_id') or not r.get('pid'):
            continue
        key = (r['session_id'], r['pid'])
        msg = r.get('message', '')
        if r.get('error_severity') == 'LOG' and 'still waiting for AccessExclusiveLock on relation ' in msg:
            waits.append((key, t))
        if r.get('state_code') == '55P03' and msg == 'canceling statement due to lock timeout':
            ends.append((key, t))
    matched = sum(any(k == key and 0 <= end - start <= 4 for k, end in ends) for key, start in waits)
    stale = sum(now - start > 5 and not any(k == key and 0 <= end - start <= 4 for k, end in ends) for key, start in waits)
    if stale:
        return 2, f'{stale} exact production exclusive wait(s) lack timely correlated timeout; inspect live chain/P5, not proof of ongoing blocking'
    if waits or ends:
        return 1, f'{len(waits)} exact production exclusive wait(s), {matched} correlated 55P03; bounded queues still delay clients; inspect client transaction/retry policy'
    return 0, 'no exact production entries exclusive waits in sampled 10min logs; live chain/defaults checked separately'

def self_test():
    now = dt.datetime(2026, 10, 21, 1, 43, 30, tzinfo=dt.timezone.utc).timestamp()
    a = dict(timestamp='2026-10-21 01:43:17.744 UTC', dbname='ledger', user='migrator', pid=59223, session_id='s', statement='LOCK TABLE public.entries IN ACCESS EXCLUSIVE MODE', error_severity='LOG', message='process 59223 still waiting for AccessExclusiveLock on relation 104379 of database 16434 after 1000.065 ms')
    e = dict(a, timestamp='2026-10-21 01:43:19.744 UTC', error_severity='ERROR', state_code='55P03', message='canceling statement due to lock timeout')
    assert classify([a, e], now)[0] == 1
    assert classify([a], now)[0] == 2
    assert classify([a, dict(e, session_id='wrong')], now)[0] == 2
    assert classify([dict(a, application_name='raiden-p5-scratch-proof'), e], now)[0] == 1
    assert classify([dict(a, user='scratch_p5_migrator'), dict(e, user='scratch_p5_migrator')], now)[0] == 0
    assert classify([], now)[0] == 0
    print('SELF TEST OK: bounded WARN, unmatched/wrong-session CRIT, app-name not suppression, scratch/empty OK')

if __name__ == '__main__':
    if sys.argv[1:] == ['--self-test']:
        self_test()
        sys.exit(0)
    try:
        paths = sorted(glob.glob('/var/log/postgresql/raiden-*.json'))[-2:]
        if not paths:
            raise OSError('logs missing')
        rows = []
        for path in paths:
            with open(path, 'rb') as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 2097152))
                if size > 2097152:
                    f.readline()
                for line in f:
                    try:
                        rows.append(json.loads(line))
                    except (ValueError, UnicodeError):
                        pass
        rc, message = classify(rows, time.time())
    except OSError:
        rc, message = 1, 'log sampling unavailable; inspect manually'
    print(f'{["OK", "WARN", "CRIT"][rc]} lock-wait-history: {message}')
    sys.exit(rc)
