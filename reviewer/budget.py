"""Durable one-per-hour review reservations within a rolling-day ceiling."""
import json
import math
import uuid
from pathlib import Path


HOUR = 3600
DAY = 86400
# The daily ceiling was sized for a human approving each draft by hand. Jev now decides
# them, so the one-per-hour reservation is the real governor and this is set to let a
# full day of hourly reviews through rather than binding first.
CAPACITY = 24


def _legacy(db):
    if db.execute("SELECT 1 FROM meta WHERE key='hourly_budget_migration'").fetchone():
        return []
    row = db.execute("SELECT value FROM meta WHERE key='next_run_after'").fetchone()
    if row is None:
        return []
    until = json.loads(row['value'])
    if not isinstance(until, (int, float)) or not math.isfinite(until):
        raise ValueError('Invalid legacy review reservation; inspect the saved queue')
    started = until - DAY
    known = {r['id'] for r in db.execute('SELECT id FROM candidates')}
    packets, uncertain = [], False
    qualification_ids = {'useful', 'unsupported', 'duplicate', 'misleading', 'addition', 'conflict'}
    for attempt in db.execute('SELECT path,state FROM attempts WHERE created>=? AND created<=?',
                              (started, started + 630)):
        if attempt['state'] in ('qualified', 'failed_editorial_check'):
            continue
        try:
            path = Path(attempt['path']) / 'packet.json'
            if path.stat().st_size > 1_000_000:
                raise ValueError('Oversized legacy packet')
            packet = json.loads(path.read_text())
            ids = [item['id'] for item in packet['candidates']]
            identities = set(ids)
            if identities == qualification_ids and len(ids) == 6:
                continue
            if not 1 <= len(ids) <= CAPACITY or len(identities) != len(ids) or not identities <= known:
                raise ValueError('Invalid legacy candidate identities')
            packets.append(identities)
        except (OSError, ValueError, KeyError, TypeError):
            uncertain = True
    trustworthy = bool(packets) and not uncertain and all(ids == packets[0] for ids in packets)
    ids = sorted(packets[0]) if trustworthy else []
    return [{'id': 'legacy-daily-batch', 'created': started,
             'units': len(ids) if trustworthy else CAPACITY, 'candidates': json.dumps(ids)}]


def _history(db):
    return [dict(r) for r in db.execute('SELECT * FROM review_reservations')] + _legacy(db)


def _status(rows, now, interval=None):
    interval = HOUR if interval is None else interval
    active = sorted((row for row in rows if row['created'] > now - DAY), key=lambda r: r['created'])
    used = sum(row['units'] for row in active)
    hourly_due = max((row['created'] + interval for row in rows), default=0)
    daily_due, remaining = 0, used
    for row in active:
        if remaining < CAPACITY:
            break
        remaining -= row['units']
        daily_due = row['created'] + DAY
    state = 'daily_limit' if used >= CAPACITY else 'hourly_limit' if now < hourly_due else 'available'
    return {'state': state, 'used_24h': used, 'remaining_24h': max(0, CAPACITY - used),
            'next_run_after': max(hourly_due, daily_due)}


def status(store, now, interval=None):
    with store.db() as db:
        return _status(_history(db), now, interval)


def reserve(store, cid, now, interval=None):
    with store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute("SELECT 1 FROM meta WHERE key='hourly_budget_migration'").fetchone():
            legacy = _legacy(db)
            for row in legacy:
                db.execute('INSERT INTO review_reservations VALUES (?,?,?,?)',
                           (row['id'], row['created'], row['units'], row['candidates']))
            marker = {'migrated_at': now, 'legacy_reservations': legacy}
            db.execute('INSERT INTO meta VALUES (?,?)', ('hourly_budget_migration', json.dumps(marker)))
            db.execute("DELETE FROM meta WHERE key='next_run_after'")
        availability = _status(_history(db), now, interval)
        if availability['state'] != 'available':
            return availability
        rid = uuid.uuid4().hex
        db.execute('INSERT INTO review_reservations VALUES (?,?,?,?)', (rid, now, 1, json.dumps([cid])))
        return {'state': 'reserved', 'reservation': rid}
