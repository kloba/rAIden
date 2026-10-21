#!/usr/bin/env python3
"""Read-only recent exact entries DROP correlation. Never suppress rejected attempts."""
import datetime as dt
import glob
import json
import sys
import time

SQL = 'DROP TABLE PUBLIC.ENTRIES'
def norm(s):
    return ' '.join(s.strip().rstrip(';').upper().split())

def classify(rows, now):
    attempts = {}
    rejected = set()
    for r in rows:
        if r.get('dbname') != 'ledger':
            continue
        try:
            stamp = dt.datetime.strptime(r['timestamp'], '%Y-%m-%d %H:%M:%S.%f UTC').replace(tzinfo=dt.timezone.utc).timestamp()
        except (KeyError, ValueError):
            continue
        if not now - 600 <= stamp <= now:
            continue
        key = (r.get('session_id'), r.get('pid'))
        msg = r.get('message', '')
        if r.get('error_severity') == 'LOG' and msg.startswith('statement: ') and norm(msg[11:]) == SQL:
            attempts.setdefault(key, []).append(stamp)
        if (r.get('state_code') == '42501' and norm(r.get('statement', '')) == SQL
            and msg == 'raiden protection: table public.entries is protected and cannot be dropped'):
            rejected.add((key, stamp))
    count = sum(len(v) for v in attempts.values())
    matched = sum(any(k == key and 0 <= end - start <= 5 for k, end in rejected)
                  for key, starts in attempts.items() for start in starts)
    unmatched = sum(now - start > 5 and not any(k == key and 0 <= end - start <= 5 for k, end in rejected)
                    for key, starts in attempts.items() for start in starts)
    if unmatched:
        return 2, f'{unmatched} exact entries DROP attempt(s) lack correlated rejection; NOT proof of commit; inspect guard/catalog/oracle'
    if count or rejected:
        return 1, f'{count} exact entries DROP attempt(s), {matched} correlated 42501 rejection(s); investigate client; never restore on LOG alone'
    return 0, 'no exact entries DROP attempts in sampled recent 10min logs; catalogue guard checked separately'

def self_test():
    now = dt.datetime(2026, 10, 21, 1, 34, 50, tzinfo=dt.timezone.utc).timestamp()
    a = dict(timestamp='2026-10-21 01:34:41.975 UTC', dbname='ledger', pid=55221, session_id='s', error_severity='LOG', message='statement: DROP TABLE public.entries')
    e = dict(a, timestamp='2026-10-21 01:34:41.978 UTC', error_severity='ERROR', state_code='42501', message='raiden protection: table public.entries is protected and cannot be dropped', statement='DROP TABLE public.entries')
    assert classify([a, e], now)[0] == 1
    assert classify([a], now)[0] == 2
    assert classify([a, dict(e, session_id='other')], now)[0] == 2
    assert classify([], now)[0] == 0
    print('SELF TEST OK: rejection WARN, unmatched/wrong-session CRIT, empty OK')

if __name__ == '__main__':
    if sys.argv[1:] == ['--self-test']:
        self_test()
        sys.exit(0)
    rows = []
    paths = sorted(glob.glob('/var/log/postgresql/raiden-*.json'))[-2:]
    try:
        if not paths:
            raise OSError('logs missing')
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
                        pass  # partial trailing JSON is normal during concurrent writes
        rc, message = classify(rows, time.time())
    except OSError:
        rc, message = 1, 'log sampling unavailable; inspect manually'
    print(f'{["OK", "WARN", "CRIT"][rc]} ddl-attempts: {message}')
    sys.exit(rc)
