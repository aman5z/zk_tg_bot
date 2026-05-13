"""
mdb_reader.py
Read-only access to Middle East Attendance Software MDB.
Table/column names verified against actual attBackup23.12.25.mdb:
  USERINFO   : USERID, Badgenumber, Name, DEFAULTDEPTID, ATT
  CHECKINOUT : USERID, CHECKTIME, SENSORID
  DEPARTMENTS: DEPTID, DEPTNAME
"""

import os
import subprocess
import configparser
import logging
from datetime import datetime, date, timedelta
from calendar import monthrange
from typing import Optional

logger = logging.getLogger(__name__)

_cfg = configparser.ConfigParser()
_cfg.read(os.path.join(os.path.dirname(__file__), 'config.ini'))

# Avoid circular import: settings.get_excluded_badges() also reads config.ini but
# mdb_reader is imported by settings indirectly through notifier.  We read the
# global excluded badges directly from _cfg here.
def _get_excluded_badges() -> set:
    raw = _cfg.get('employees', 'exclude_badges', fallback='')
    return {b.strip() for b in raw.split(',') if b.strip()}

# ─── Config ───────────────────────────────────────────────────────────────────

def _get_mdb_path() -> str:
    return _cfg.get('mdb', 'path', fallback='').strip()

def _get_excluded_depts() -> list:
    raw = _cfg.get('departments', 'exclude', fallback='')
    return [d.strip().upper() for d in raw.split(',') if d.strip()]

def set_mdb_path(new_path: str):
    if 'mdb' not in _cfg:
        _cfg['mdb'] = {}
    _cfg['mdb']['path'] = new_path
    with open(os.path.join(os.path.dirname(__file__), 'config.ini'), 'w') as f:
        _cfg.write(f)
    refresh_dept_cache()   # invalidate so next query loads from new MDB
    logger.info(f"MDB path updated: {new_path}")

# ─── Path resolution ──────────────────────────────────────────────────────────

def _resolve_local_path() -> Optional[str]:
    raw = _get_mdb_path()
    if not raw:
        return None
    # Direct local/WSL path
    if os.path.isfile(raw):
        return raw
    # UNC → try mount point
    if raw.startswith('//') or raw.startswith('\\\\'):
        mount = _cfg.get('mdb', 'mount_point', fallback='/mnt/attdb').strip()
        parts = raw.lstrip('/').split('/', 2)
        if len(parts) == 3:
            local = os.path.join(mount, parts[2])
            if os.path.isfile(local):
                return local
    return None

# ─── mdbtools core ────────────────────────────────────────────────────────────

def _mdb_export(table: str) -> list:
    mdb = _resolve_local_path()
    if not mdb:
        raise RuntimeError("MDB not accessible. Check /mdbinfo or use /setmdb.")
    r = subprocess.run(['mdb-export', mdb, table],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"mdb-export error on {table}: {r.stderr.strip()}")
    lines = r.stdout.strip().splitlines()
    if not lines:
        return []
    headers = [h.strip().strip('"') for h in lines[0].split(',')]
    rows = []
    for line in lines[1:]:
        vals = _csv_split(line)
        if len(vals) == len(headers):
            rows.append(dict(zip(headers, vals)))
    return rows

def _csv_split(line: str) -> list:
    result, cur, in_q = [], '', False
    for ch in line:
        if ch == '"':
            in_q = not in_q
        elif ch == ',' and not in_q:
            result.append(cur.strip().strip('"'))
            cur = ''
        else:
            cur += ch
    result.append(cur.strip().strip('"'))
    return result

def _parse_dt(s: str) -> Optional[datetime]:
    # CHECKINOUT format: "11/20/18 06:50:09" or "2026/05/13 07:32:14"
    for fmt in ('%m/%d/%y %H:%M:%S', '%Y/%m/%d %H:%M:%S',
                '%m/%d/%Y %H:%M:%S', '%Y-%m-%d %H:%M:%S',
                '%d/%m/%Y %H:%M:%S'):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None

def list_tables() -> list:
    mdb = _resolve_local_path()
    if not mdb:
        raise RuntimeError("MDB not accessible.")
    r = subprocess.run(['mdb-tables', '-1', mdb],
                       capture_output=True, text=True, timeout=10)
    return [t.strip() for t in r.stdout.splitlines() if t.strip()]

# ─── MDB info ─────────────────────────────────────────────────────────────────

def get_mdb_info() -> dict:
    raw = _get_mdb_path()
    local = _resolve_local_path()
    info = {
        'configured_path': raw,
        'accessible': local is not None,
        'local_path': local or 'Not accessible',
        'size_mb': None, 'last_modified': None,
    }
    if local and os.path.isfile(local):
        st = os.stat(local)
        info['size_mb'] = round(st.st_size / 1048576, 1)
        info['last_modified'] = datetime.fromtimestamp(
            st.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
    return info

# ─── Departments (cached) ─────────────────────────────────────────────────────

_dept_cache: dict = {}   # DEPTID → DEPTNAME

def _get_dept_map() -> dict:
    global _dept_cache
    if _dept_cache:
        return _dept_cache
    try:
        rows = _mdb_export('DEPARTMENTS')
        _dept_cache = {r['DEPTID']: r['DEPTNAME'] for r in rows
                       if r.get('DEPTID') and r.get('DEPTNAME')}
    except Exception as e:
        logger.warning(f"Could not load DEPARTMENTS: {e}")
    return _dept_cache

def refresh_dept_cache():
    global _dept_cache
    _dept_cache = {}
    _get_dept_map()


def get_dept_map() -> dict:
    """Public wrapper around the cached department map."""
    return _get_dept_map()

# ─── Employees ────────────────────────────────────────────────────────────────

def get_employees(active_only: bool = True) -> list:
    excluded_depts   = _get_excluded_depts()
    excluded_badges  = _get_excluded_badges()
    dept_map = _get_dept_map()
    rows = _mdb_export('USERINFO')
    result = []
    for r in rows:
        badge  = r.get('Badgenumber', '').strip()
        name   = r.get('Name', '').strip()
        uid    = r.get('USERID', '').strip()
        dept_id = r.get('DEFAULTDEPTID', '').strip()
        dept   = dept_map.get(dept_id, dept_id)   # resolve ID → name
        active = r.get('ATT', '1').strip()

        if not badge or not name:
            continue
        if badge in excluded_badges:
            continue
        if dept.upper() in excluded_depts:
            continue
        if active_only and active == '0':
            continue

        result.append({
            'uid': uid,           # internal USERID (used in CHECKINOUT)
            'badge': badge,       # Badgenumber (shown to users)
            'name': name,
            'dept': dept,
            'active': active != '0',
        })
    return result

def search_employee(query: str) -> list:
    q = query.lower().strip()
    return [e for e in get_employees(active_only=False)
            if q in e['name'].lower() or q in e['badge'].lower()]

# ─── Attendance ───────────────────────────────────────────────────────────────

def get_attendance(date_from: date, date_to: date,
                   uid: str = None, badge: str = None) -> list:
    """
    Fetch punch records. Filter by uid (USERID) or badge (Badgenumber).
    CHECKINOUT.USERID matches USERINFO.USERID — not Badgenumber.
    """
    # If badge given, resolve to uid first
    if badge and not uid:
        emps = get_employees(active_only=False)
        emp = next((e for e in emps if e['badge'] == badge.strip()), None)
        if emp:
            uid = emp['uid']
        else:
            return []   # badge not found

    rows = _mdb_export('CHECKINOUT')
    result = []
    for r in rows:
        r_uid = r.get('USERID', '').strip()
        chk   = r.get('CHECKTIME', '').strip()
        dev   = r.get('SENSORID', '').strip()

        if not r_uid or not chk:
            continue
        if uid and r_uid != uid:
            continue

        dt = _parse_dt(chk)
        if not dt:
            continue
        if dt.date() < date_from or dt.date() > date_to:
            continue

        result.append({
            'uid': r_uid,
            'timestamp': dt,
            'date': dt.date(),
            'time': dt.strftime('%H:%M:%S'),
            'device': dev,
        })
    result.sort(key=lambda x: x['timestamp'])
    return result

# ─── Build uid→employee map ───────────────────────────────────────────────────

def _uid_map() -> dict:
    """Return dict of uid → employee dict."""
    return {e['uid']: e for e in get_employees(active_only=False)}

# ─── Today ────────────────────────────────────────────────────────────────────

def get_today_summary() -> dict:
    today = date.today()
    emps = get_employees(active_only=True)
    punches = get_attendance(today, today)
    excluded = _get_excluded_depts()

    punched_uids = {p['uid'] for p in punches}
    present, absent, dept_stats = [], [], {}

    for e in emps:
        if e['dept'].upper() in excluded:
            continue
        d = e['dept'] or 'Unknown'
        dept_stats.setdefault(d, {'present': 0, 'absent': 0})
        if e['uid'] in punched_uids:
            present.append(e)
            dept_stats[d]['present'] += 1
        else:
            absent.append(e)
            dept_stats[d]['absent'] += 1

    return {
        'date': today.strftime('%A, %d %B %Y'),
        'present': present, 'absent': absent,
        'present_count': len(present), 'absent_count': len(absent),
        'total': len(present) + len(absent),
        'dept_stats': dept_stats, 'punches': punches,
    }

# ─── History ──────────────────────────────────────────────────────────────────

def get_history(date_from: date, date_to: date) -> list:
    emps = get_employees(active_only=True)
    punches = get_attendance(date_from, date_to)
    excluded = _get_excluded_depts()
    filtered = [e for e in emps if e['dept'].upper() not in excluded]

    by_date = {}
    for p in punches:
        by_date.setdefault(p['date'], set()).add(p['uid'])

    result, cur = [], date_from
    while cur <= date_to:
        punched = by_date.get(cur, set())
        present = [e for e in filtered if e['uid'] in punched]
        absent  = [e for e in filtered if e['uid'] not in punched]
        result.append({
            'date': cur, 'date_str': cur.strftime('%d/%m/%Y'),
            'weekday': cur.strftime('%A'),
            'is_weekend': cur.weekday() in (4, 5),
            'present': present, 'absent': absent,
            'present_count': len(present), 'absent_count': len(absent),
        })
        cur += timedelta(days=1)
    return result

# ─── Employee calendar ────────────────────────────────────────────────────────

def get_employee_calendar(badge: str, year: int, month: int) -> dict:
    first = date(year, month, 1)
    last  = date(year, month, monthrange(year, month)[1])

    emps = get_employees(active_only=False)
    emp = next((e for e in emps if e['badge'] == badge), None)
    if not emp:
        raise ValueError(f"Badge {badge} not found in MDB.")

    punches = get_attendance(first, last, uid=emp['uid'])

    by_date = {}
    for p in punches:
        by_date.setdefault(p['date'], []).append(p['time'])

    days, cur = [], first
    while cur <= last:
        day_punches = by_date.get(cur, [])
        is_weekend  = cur.weekday() in (4, 5)
        days.append({
            'date': cur, 'date_str': cur.strftime('%d'),
            'weekday': cur.strftime('%a'),
            'punches': day_punches,
            'present': bool(day_punches),
            'is_weekend': is_weekend,
        })
        cur += timedelta(days=1)

    working = [d for d in days if not d['is_weekend']]
    return {
        'badge': badge, 'name': emp['name'],
        'month': first.strftime('%B %Y'), 'days': days,
        'present_days': sum(1 for d in working if d['present']),
        'absent_days':  sum(1 for d in working if not d['present']),
        'working_days': len(working),
    }

# ─── Late arrivals ────────────────────────────────────────────────────────────

def get_late_today(shift_start: str = None) -> list:
    if shift_start is None:
        shift_start = _cfg.get('attendance', 'shift_start', fallback='07:30')
    today = date.today()
    punches = get_attendance(today, today)
    uid_map = _uid_map()
    excluded = _get_excluded_depts()
    shift_t = datetime.strptime(shift_start, '%H:%M').time()

    first = {}
    for p in punches:
        first.setdefault(p['uid'], p)

    late = []
    for uid, p in first.items():
        emp = uid_map.get(uid)
        if not emp or emp['dept'].upper() in excluded:
            continue
        pt = datetime.strptime(p['time'], '%H:%M:%S').time()
        if pt > shift_t:
            diff = (datetime.combine(today, pt) -
                    datetime.combine(today, shift_t))
            late.append({
                'badge': emp['badge'], 'name': emp['name'],
                'dept': emp['dept'], 'punch_time': p['time'],
                'late_mins': int(diff.total_seconds() / 60),
            })
    return sorted(late, key=lambda x: x['late_mins'], reverse=True)

# ─── Top absent ───────────────────────────────────────────────────────────────

def get_top_absent(n: int = 10,
                   date_from: date = None, date_to: date = None) -> list:
    today = date.today()
    if not date_from:
        date_from = today.replace(day=1)
    if not date_to:
        date_to = today

    emps = get_employees(active_only=True)
    punches = get_attendance(date_from, date_to)
    excluded = _get_excluded_depts()

    punched_days = {}
    for p in punches:
        punched_days.setdefault(p['uid'], set()).add(p['date'])

    total = (date_to - date_from).days + 1
    working = sum(1 for i in range(total)
                  if (date_from + timedelta(days=i)).weekday() not in (4, 5))

    result = []
    for e in emps:
        if e['dept'].upper() in excluded:
            continue
        present = len(punched_days.get(e['uid'], set()))
        absent  = max(0, working - present)
        result.append({
            'badge': e['badge'], 'name': e['name'], 'dept': e['dept'],
            'absent_days': absent, 'present_days': present,
            'working_days': working,
        })
    return sorted(result, key=lambda x: x['absent_days'], reverse=True)[:n]

# ─── Who is in ────────────────────────────────────────────────────────────────

def get_who_is_in() -> list:
    today = date.today()
    punches = get_attendance(today, today)
    uid_map = _uid_map()
    excluded = _get_excluded_depts()

    by_uid = {}
    for p in punches:
        by_uid.setdefault(p['uid'], []).append(p['time'])

    result = []
    for uid, times in by_uid.items():
        emp = uid_map.get(uid)
        if not emp or emp['dept'].upper() in excluded:
            continue
        if len(times) % 2 == 1:
            result.append({
                'badge': emp['badge'], 'name': emp['name'],
                'dept': emp['dept'], 'last_punch': times[-1],
                'punch_count': len(times),
            })
    return result

# ─── Punch feed ───────────────────────────────────────────────────────────────

def get_punch_feed(n: int = 20) -> list:
    today = date.today()
    punches = get_attendance(today, today)
    uid_map = _uid_map()
    result = []
    for p in sorted(punches, key=lambda x: x['timestamp'], reverse=True)[:n]:
        emp = uid_map.get(p['uid'])
        result.append({
            'badge': emp['badge'] if emp else p['uid'],
            'name':  emp['name']  if emp else 'Unknown',
            'dept':  emp['dept']  if emp else '—',
            'time':  p['time'], 'device': p['device'],
        })
    return result

# ─── Week / month ─────────────────────────────────────────────────────────────

def get_week_summary() -> list:
    """Return Sun → today. UAE work week is Sun–Thu; weekends are Fri+Sat."""
    today = date.today()
    # weekday(): Mon=0 … Sun=6  →  days since Sunday = (weekday+1) % 7
    days_since_sun = (today.weekday() + 1) % 7
    week_start = today - timedelta(days=days_since_sun)
    return get_history(week_start, today)

def get_month_dept_summary() -> list:
    today = date.today()
    history = get_history(today.replace(day=1), today)
    excluded = _get_excluded_depts()
    stats = {}
    for day in history:
        if day['is_weekend']:
            continue
        for e in day['present']:
            if e['dept'].upper() in excluded:
                continue
            stats.setdefault(e['dept'], {'p': 0, 't': 0})
            stats[e['dept']]['p'] += 1
            stats[e['dept']]['t'] += 1
        for e in day['absent']:
            if e['dept'].upper() in excluded:
                continue
            stats.setdefault(e['dept'], {'p': 0, 't': 0})
            stats[e['dept']]['t'] += 1
    result = []
    for dept, s in sorted(stats.items()):
        pct = round(s['p'] / s['t'] * 100, 1) if s['t'] else 0
        result.append({'dept': dept, 'percent': pct,
                       'present': s['p'], 'total': s['t']})
    return sorted(result, key=lambda x: x['percent'])

# ─── DB stats ─────────────────────────────────────────────────────────────────

def get_db_stats() -> dict:
    info = get_mdb_info()
    try:
        emps = get_employees(active_only=False)
        active = sum(1 for e in emps if e['active'])
    except Exception:
        emps, active = [], 0
    return {
        'mdb_path': info['configured_path'],
        'accessible': info['accessible'],
        'size_mb': info['size_mb'],
        'last_modified': info['last_modified'],
        'total_employees': len(emps),
        'active_employees': active,
    }

# ─── Employee punches (by badge, date range) ──────────────────────────────────

def get_employee_punches(badge: str, date_from: date, date_to: date) -> list:
    return get_attendance(date_from, date_to, badge=badge)


# ─── Latest punches per device (SENSORID) ────────────────────────────────────

def get_latest_punches_per_device(n: int = 2, days_back: int = 3) -> dict:
    """
    Return the most recent `n` punch records for each device (SENSORID) found
    in the MDB over the last `days_back` days.

    Returns dict: sensorid → list of punch dicts (newest first, at most n each).
    Each punch includes resolved employee info (badge, name, dept).
    """
    today     = date.today()
    date_from = today - timedelta(days=days_back - 1)
    punches   = get_attendance(date_from, today)
    uid_map   = _uid_map()

    by_device: dict = {}
    # Sort newest-first so we naturally take the first n per device
    for p in sorted(punches, key=lambda x: x['timestamp'], reverse=True):
        dev = p['device'] or 'unknown'
        bucket = by_device.setdefault(dev, [])
        if len(bucket) < n:
            emp = uid_map.get(p['uid'])
            bucket.append({
                'badge':     emp['badge'] if emp else p['uid'],
                'name':      emp['name']  if emp else 'Unknown',
                'dept':      emp['dept']  if emp else '—',
                'timestamp': p['timestamp'],
                'time':      p['time'],
                'date':      p['date'],
                'device':    dev,
            })
    return by_device

def get_latest_punches(n: int = 10, days_back: int = 3) -> list:
    """
    Return the most recent `n` punch records from the last `days_back` days.
    Each record includes employee info (badge, name, dept) and timestamp/device.
    """
    today = date.today()
    date_from = today - timedelta(days=days_back - 1)
    punches = get_attendance(date_from, today)
    uid_map = _uid_map()

    recent = sorted(punches, key=lambda x: x['timestamp'], reverse=True)[:n]
    result = []
    for p in recent:
        emp = uid_map.get(p['uid'])
        result.append({
            'badge':     emp['badge'] if emp else p['uid'],
            'name':      emp['name']  if emp else 'Unknown',
            'dept':      emp['dept']  if emp else '—',
            'timestamp': p['timestamp'],
            'time':      p['time'],
            'date':      p['date'],
            'device':    p['device'],
        })
    return result


# ─── Live punch feed (punches since a given timestamp) ────────────────────────

def get_live_punches_since(since_ts: datetime) -> list:
    """
    Return all punch records after `since_ts` (today and yesterday only).
    Used by the live-punch notification loop.
    """
    today = date.today()
    yesterday = today - timedelta(days=1)
    punches = get_attendance(yesterday, today)
    uid_map = _uid_map()

    result = []
    for p in sorted(punches, key=lambda x: x['timestamp']):
        if p['timestamp'] <= since_ts:
            continue
        emp = uid_map.get(p['uid'])
        result.append({
            'badge':     emp['badge'] if emp else p['uid'],
            'name':      emp['name']  if emp else 'Unknown',
            'dept':      emp['dept']  if emp else '—',
            'timestamp': p['timestamp'],
            'time':      p['time'],
            'device':    p['device'],
        })
    return result
