"""
zk_devices.py
ZKTeco device control via pyzk.
Reboot, clock sync, user sync, add user, device status, unknown UIDs.
"""

import configparser
import logging
import os
from datetime import datetime
from typing import Optional
from zk import ZK
from zk.exception import ZKErrorResponse, ZKNetworkError

logger = logging.getLogger(__name__)

_cfg = configparser.ConfigParser()
_cfg.read(os.path.join(os.path.dirname(__file__), 'config.ini'))

# ─── Config ───────────────────────────────────────────────────────────────────

def _get_devices() -> list:
    ips   = [i.strip() for i in _cfg.get('devices', 'ips', fallback='').split(',') if i.strip()]
    names_raw = _cfg.get('devices', 'names', fallback='')
    names = [n.strip() for n in names_raw.split(',') if n.strip()]
    port    = _cfg.getint('devices', 'port', fallback=4370)
    timeout = _cfg.getint('devices', 'timeout', fallback=10)
    result = []
    for i, ip in enumerate(ips):
        result.append({
            'ip': ip,
            'name': names[i] if i < len(names) else f'Device {i+1}',
            'port': port,
            'timeout': timeout,
        })
    return result

def get_device_by_ip(ip: str) -> Optional[dict]:
    return next((d for d in _get_devices() if d['ip'] == ip), None)

def get_device_by_name(name: str) -> Optional[dict]:
    name_lower = name.lower().strip()
    return next((d for d in _get_devices()
                 if d['name'].lower() == name_lower), None)

# ─── Connection helper ────────────────────────────────────────────────────────

def _connect(device: dict):
    """Connect to a ZK device. Returns (conn, zk) or raises."""
    zk = ZK(device['ip'], port=device['port'],
            timeout=device['timeout'], password=0,
            force_udp=False, ommit_ping=True)
    conn = zk.connect()
    return conn, zk

# ─── Device status ────────────────────────────────────────────────────────────

def get_device_status() -> list:
    """Ping all devices, return status list."""
    devices = _get_devices()
    result = []
    for d in devices:
        status = {'name': d['name'], 'ip': d['ip'], 'online': False,
                  'users': None, 'logs': None, 'firmware': None, 'time': None}
        conn = None
        try:
            conn, zk = _connect(d)
            status['online'] = True
            try:
                users = conn.get_users()
                status['users'] = len(users)
            except Exception:
                pass
            try:
                status['time'] = conn.get_time().strftime('%H:%M:%S')
            except Exception:
                pass
        except Exception as e:
            status['error'] = str(e)
        finally:
            if conn:
                try:
                    conn.enable_device()
                    conn.disconnect()
                except Exception:
                    pass
        result.append(status)
    return result

# ─── Clock sync ───────────────────────────────────────────────────────────────

def sync_clocks() -> list:
    """Sync all device clocks to current system time."""
    devices = _get_devices()
    now = datetime.now()
    results = []
    for d in devices:
        r = {'name': d['name'], 'ip': d['ip'], 'ok': False}
        try:
            conn, zk = _connect(d)
            conn.set_time(now)
            r['ok'] = True
            r['time_set'] = now.strftime('%Y-%m-%d %H:%M:%S')
            try:
                conn.enable_device()
                conn.disconnect()
            except Exception:
                pass
        except Exception as e:
            r['error'] = str(e)
        results.append(r)
    return results

# ─── Reboot ───────────────────────────────────────────────────────────────────

def reboot_all() -> list:
    """Reboot all configured devices."""
    devices = _get_devices()
    results = []
    for d in devices:
        conn = None
        try:
            conn, zk = _connect(d)
            conn.restart()
            results.append({'ok': True, 'name': d['name'], 'ip': d['ip']})
        except Exception as e:
            results.append({'ok': False, 'name': d['name'], 'ip': d['ip'], 'error': str(e)})
        finally:
            if conn:
                try:
                    conn.disconnect()
                except Exception:
                    pass
    return results

def reboot_device(ip: str) -> dict:
    """Reboot a single device by IP."""
    d = get_device_by_ip(ip)
    if not d:
        # Try by name
        d = get_device_by_name(ip)
    if not d:
        return {'ok': False, 'error': f'Device not found: {ip}'}
    try:
        conn, zk = _connect(d)
        conn.restart()
        return {'ok': True, 'name': d['name'], 'ip': d['ip']}
    except Exception as e:
        return {'ok': False, 'name': d.get('name', ip), 'ip': ip, 'error': str(e)}

# ─── User sync ────────────────────────────────────────────────────────────────

def sync_users() -> list:
    """
    Sync users across all devices.
    Collects users from all online devices, pushes missing ones to each device.
    """
    devices = _get_devices()
    all_users = {}   # uid → user object
    device_users = {}  # ip → set of uids

    results = []

    failed_ips = set()   # IPs that errored in phase 1

    # Step 1: collect all users from all devices
    for d in devices:
        try:
            conn, zk = _connect(d)
            users = conn.get_users()
            device_users[d['ip']] = {str(u.user_id) for u in users}
            for u in users:
                uid = str(u.user_id)
                if uid not in all_users:
                    all_users[uid] = u
            try:
                conn.enable_device()
                conn.disconnect()
            except Exception:
                pass
        except Exception as e:
            failed_ips.add(d['ip'])
            device_users[d['ip']] = set()
            results.append({'name': d['name'], 'ip': d['ip'],
                            'ok': False, 'error': str(e), 'pushed': 0})

    # Step 2: push missing users to each device
    for d in devices:
        if d['ip'] in failed_ips:
            continue  # already logged error in phase 1
        missing = [all_users[uid] for uid in all_users
                   if uid not in device_users[d['ip']]]
        if not missing:
            results.append({'name': d['name'], 'ip': d['ip'],
                            'ok': True, 'pushed': 0, 'note': 'Already in sync'})
            continue
        pushed = 0
        try:
            conn, zk = _connect(d)
            for u in missing:
                try:
                    conn.save_user(
                        uid=u.uid, name=u.name,
                        privilege=u.privilege,
                        password=u.password or '',
                        group_id=u.group_id or '',
                        user_id=u.user_id,
                    )
                    pushed += 1
                except Exception as ue:
                    logger.warning(f"Push user {u.user_id} to {d['ip']}: {ue}")
            try:
                conn.enable_device()
                conn.disconnect()
            except Exception:
                pass
            results.append({'name': d['name'], 'ip': d['ip'],
                            'ok': True, 'pushed': pushed,
                            'missing_found': len(missing)})
        except Exception as e:
            results.append({'name': d['name'], 'ip': d['ip'],
                            'ok': False, 'error': str(e), 'pushed': pushed})
    return results

# ─── Add user ─────────────────────────────────────────────────────────────────

def add_user(badge: str, name: str, privilege: int = 0,
             target_ips: list = None) -> list:
    """
    Add a new user to ZK devices.
    privilege: 0=user, 2=enroller, 6=manager, 14=admin
    target_ips: list of IPs to add to; None = all devices
    Note: Fingerprint/face enrollment must be done physically on device.
    Middle East Attendance Software will pick up the new user on next sync.
    """
    devices = _get_devices()
    if target_ips:
        devices = [d for d in devices if d['ip'] in target_ips]

    results = []
    for d in devices:
        r = {'name': d['name'], 'ip': d['ip'], 'ok': False}
        try:
            conn, zk = _connect(d)
            # uid is auto-assigned; user_id = badge number
            conn.save_user(
                uid=int(badge),
                name=name[:24],  # ZK devices limit name length
                privilege=privilege,
                password='',
                group_id='',
                user_id=badge,
            )
            r['ok'] = True
            r['note'] = 'User added. Biometrics must be enrolled on device.'
            try:
                conn.enable_device()
                conn.disconnect()
            except Exception:
                pass
        except Exception as e:
            r['error'] = str(e)
        results.append(r)
    return results

# ─── Unknown users ────────────────────────────────────────────────────────────

def get_unknown_users(known_badges: set) -> list:
    """
    Return users on devices whose badge is not in known_badges (from MDB).
    """
    devices = _get_devices()
    unknown = []
    for d in devices:
        try:
            conn, zk = _connect(d)
            users = conn.get_users()
            for u in users:
                if str(u.user_id) not in known_badges:
                    unknown.append({
                        'device_name': d['name'],
                        'device_ip': d['ip'],
                        'uid': u.uid,
                        'user_id': u.user_id,
                        'name_on_device': u.name,
                    })
            try:
                conn.enable_device()
                conn.disconnect()
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"Unknown users check failed for {d['ip']}: {e}")
    return unknown
