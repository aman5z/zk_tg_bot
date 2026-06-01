"""
Attendance sync service: pull logs from configured ZKTeco devices and append
attendance records to MDB CHECKINOUT safely.
"""

from datetime import datetime
from typing import Optional

import mdb_reader
import settings
import zk_devices


def _normalize_event(log, device: dict) -> Optional[dict]:
    ts = getattr(log, 'timestamp', None)
    user_id = str(getattr(log, 'user_id', '') or '').strip()
    if not user_id or not isinstance(ts, datetime):
        return None
    return {
        'userid': user_id,
        'checktime': ts,
        'sensorid': str(device.get('sensor_id', '') or ''),
        'checktype': getattr(log, 'punch', None) if getattr(log, 'punch', None) is not None else 'I',
        'verifycode': int(getattr(log, 'status', 0) or 0),
        'workcode': int(getattr(log, 'workcode', 0) or 0),
        'sn': str(device.get('ip', '') or ''),
    }


def _select_device(selector: str, devices: list) -> Optional[dict]:
    q = (selector or '').strip().lower()
    if not q:
        return None
    for d in devices:
        if d.get('ip', '').lower() == q:
            return d
    for d in devices:
        if d.get('sensor_id', '').lower() == q:
            return d
    for d in devices:
        if d.get('name', '').strip().lower() == q:
            return d
    return None


def sync_devices(devices: list) -> dict:
    summary = {
        'devices_contacted': 0,
        'records_fetched': 0,
        'inserted': 0,
        'duplicates': 0,
        'failed': [],
        'devices': [],
    }
    for device in devices:
        row = {
            'name': device.get('name', ''),
            'ip': device.get('ip', ''),
            'sensor_id': device.get('sensor_id', ''),
            'ok': False,
            'fetched': 0,
            'inserted': 0,
            'duplicates': 0,
        }
        summary['devices_contacted'] += 1
        try:
            logs = zk_devices.get_device_attendance_logs(device)
            row['fetched'] = len(logs)
            summary['records_fetched'] += row['fetched']
            events = [ev for ev in (_normalize_event(log, device) for log in logs) if ev]
            write_res = mdb_reader.insert_attendance_events(events)
            row['inserted'] = int(write_res.get('inserted', 0))
            row['duplicates'] = int(write_res.get('duplicates', 0))
            summary['inserted'] += row['inserted']
            summary['duplicates'] += row['duplicates']
            row['ok'] = True
        except Exception as exc:
            row['error'] = str(exc)
            summary['failed'].append({
                'name': row['name'],
                'ip': row['ip'],
                'error': row['error'],
            })
        summary['devices'].append(row)
    return summary


def sync_all_devices() -> dict:
    devices = settings.get_devices()
    return sync_devices(devices)


def sync_one_device(selector: str) -> dict:
    devices = settings.get_devices()
    device = _select_device(selector, devices)
    if not device:
        raise RuntimeError(f'Configured device not found for "{selector}"')
    return sync_devices([device])
