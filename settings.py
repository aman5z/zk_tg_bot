"""
settings.py
Centralized runtime settings with config.ini persistence.
All read/write goes through this module so every component sees consistent values.
"""

import configparser
import logging
import os

logger = logging.getLogger(__name__)

_CFG_PATH = os.path.join(os.path.dirname(__file__), 'config.ini')
_cfg = configparser.ConfigParser()
_cfg.read(_CFG_PATH)


def _save():
    try:
        with open(_CFG_PATH, 'w') as f:
            _cfg.write(f)
    except OSError as e:
        logger.error(f"Failed to persist settings to {_CFG_PATH}: {e}")


def _ensure(section: str):
    if section not in _cfg:
        _cfg[section] = {}


# ─── Global employee exclusions ───────────────────────────────────────────────

def get_excluded_badges() -> set:
    raw = _cfg.get('employees', 'exclude_badges', fallback='')
    return {b.strip() for b in raw.split(',') if b.strip()}


def set_excluded_badges(badges: set):
    _ensure('employees')
    _cfg['employees']['exclude_badges'] = ','.join(sorted(badges))
    _save()


# ─── On-demand /report settings ───────────────────────────────────────────────

def get_report_departments() -> str:
    """'ALL' or comma-separated dept names."""
    return _cfg.get('report_settings', 'departments', fallback='ALL')


def set_report_departments(val: str):
    _ensure('report_settings')
    _cfg['report_settings']['departments'] = val
    _save()


def get_report_formats() -> str:
    """Comma-separated: xlsx, png, pdf, all."""
    return _cfg.get('report_settings', 'formats', fallback='xlsx')


def set_report_formats(val: str):
    _ensure('report_settings')
    _cfg['report_settings']['formats'] = val
    _save()


def get_report_template() -> str:
    return _cfg.get('report_settings', 'template', fallback='default')


def set_report_template(val: str):
    _ensure('report_settings')
    _cfg['report_settings']['template'] = val
    _save()


# ─── Daily report settings ────────────────────────────────────────────────────

def get_daily_hour() -> int:
    return _cfg.getint('notifications', 'daily_report_hour', fallback=8)


def set_daily_hour(val: int):
    _ensure('notifications')
    _cfg['notifications']['daily_report_hour'] = str(val)
    _save()


def get_daily_minute() -> int:
    return _cfg.getint('notifications', 'daily_report_minute', fallback=15)


def set_daily_minute(val: int):
    _ensure('notifications')
    _cfg['notifications']['daily_report_minute'] = str(val)
    _save()


def get_daily_days() -> str:
    """Comma-separated day numbers: 0=Mon…6=Sun. UAE default: 0,1,2,3,6."""
    return _cfg.get('daily_report', 'days', fallback='0,1,2,3,6')


def set_daily_days(val: str):
    _ensure('daily_report')
    _cfg['daily_report']['days'] = val
    _save()


def get_daily_departments() -> str:
    return _cfg.get('daily_report', 'departments', fallback='ALL')


def set_daily_departments(val: str):
    _ensure('daily_report')
    _cfg['daily_report']['departments'] = val
    _save()


def get_daily_exclude_badges() -> str:
    """Extra badge exclusions for daily report (beyond global exclude_badges)."""
    return _cfg.get('daily_report', 'exclude_badges', fallback='')


def set_daily_exclude_badges(val: str):
    _ensure('daily_report')
    _cfg['daily_report']['exclude_badges'] = val
    _save()


def get_daily_formats() -> str:
    return _cfg.get('daily_report', 'formats', fallback='xlsx')


def set_daily_formats(val: str):
    _ensure('daily_report')
    _cfg['daily_report']['formats'] = val
    _save()


def get_daily_template() -> str:
    return _cfg.get('daily_report', 'template', fallback='default')


def set_daily_template(val: str):
    _ensure('daily_report')
    _cfg['daily_report']['template'] = val
    _save()


def get_daily_save_dir() -> str:
    """Local directory where daily report files are saved (empty = disabled)."""
    return _cfg.get('daily_report', 'save_dir', fallback='').strip()


def set_daily_save_dir(val: str):
    _ensure('daily_report')
    _cfg['daily_report']['save_dir'] = val
    _save()


# ─── On-demand /report save directory ────────────────────────────────────────

def get_report_save_dir() -> str:
    """Local directory where on-demand /report files are saved (empty = disabled)."""
    return _cfg.get('report_settings', 'save_dir', fallback='').strip()


def set_report_save_dir(val: str):
    _ensure('report_settings')
    _cfg['report_settings']['save_dir'] = val
    _save()


# ─── Live punch notifications ─────────────────────────────────────────────────

def get_live_punches() -> bool:
    return _cfg.getboolean('notifications', 'notify_punches', fallback=False)


def set_live_punches(val: bool):
    _ensure('notifications')
    _cfg['notifications']['notify_punches'] = '1' if val else '0'
    _save()


# ─── Device settings ───────────────────────────────────────────────────────────

def get_device_timeout() -> int:
    return _cfg.getint('devices', 'timeout', fallback=10)


def set_device_timeout(val: int):
    _ensure('devices')
    _cfg['devices']['timeout'] = str(max(1, int(val)))
    _save()


def get_devices() -> list:
    _ensure('devices')
    ips = [i.strip() for i in _cfg.get('devices', 'ips', fallback='').split(',')
           if i.strip()]
    names = [n.strip() for n in _cfg.get('devices', 'names', fallback='').split(',')
             if n.strip()]
    default_port = _cfg.getint('devices', 'port', fallback=4370)
    ports_raw = [p.strip() for p in _cfg.get('devices', 'ports', fallback='').split(',')
                 if p.strip()]
    timeout = get_device_timeout()
    devices = []
    for i, ip in enumerate(ips):
        port = default_port
        if i < len(ports_raw):
            try:
                port = int(ports_raw[i])
            except ValueError:
                port = default_port
        devices.append({
            'ip': ip,
            'name': names[i] if i < len(names) else f'Device {i + 1}',
            'port': port,
            'timeout': timeout,
        })
    return devices


def save_devices(devices: list):
    _ensure('devices')
    clean = []
    for i, dev in enumerate(devices):
        clean.append({
            'ip': str(dev.get('ip', '')).strip(),
            'name': str(dev.get('name', '')).strip() or f'Device {i + 1}',
            'port': max(1, int(dev.get('port', 4370))),
        })
    _cfg['devices']['ips'] = ','.join(d['ip'] for d in clean)
    _cfg['devices']['names'] = ','.join(d['name'] for d in clean)
    _cfg['devices']['ports'] = ','.join(str(d['port']) for d in clean)
    _cfg['devices']['port'] = str(clean[0]['port'] if clean else 4370)
    _save()


# ─── Read-only telegram/device helpers for notifier ──────────────────────────

def get_chat_id() -> str:
    return _cfg.get('telegram', 'chat_id', fallback='').strip()


# ─── Summary helpers ──────────────────────────────────────────────────────────

_DAY_NAMES = {0: 'Mon', 1: 'Tue', 2: 'Wed', 3: 'Thu',
              4: 'Fri', 5: 'Sat', 6: 'Sun'}


def daily_days_label() -> str:
    days = [d.strip() for d in get_daily_days().split(',') if d.strip()]
    return ', '.join(_DAY_NAMES.get(int(d), d) for d in days) or '—'


def daily_time_label() -> str:
    return f"{get_daily_hour():02d}:{get_daily_minute():02d}"


# ─── SMTP / Email settings ────────────────────────────────────────────────────

def get_smtp_enabled() -> bool:
    """Master switch — False by default so pure Telegram users are unaffected."""
    return _cfg.getboolean('smtp', 'enabled', fallback=False)


def set_smtp_enabled(val: bool):
    _ensure('smtp')
    _cfg['smtp']['enabled'] = '1' if val else '0'
    _save()


def get_smtp_daily_enabled() -> bool:
    """Send email for the scheduled daily report when True."""
    return _cfg.getboolean('smtp', 'daily_email_enabled', fallback=False)


def set_smtp_daily_enabled(val: bool):
    _ensure('smtp')
    _cfg['smtp']['daily_email_enabled'] = '1' if val else '0'
    _save()


def get_smtp_sender_email() -> str:
    return _cfg.get('smtp', 'sender_email', fallback='').strip()


def set_smtp_sender_email(val: str):
    _ensure('smtp')
    _cfg['smtp']['sender_email'] = val.strip()
    _save()


def get_smtp_sender_name() -> str:
    return _cfg.get('smtp', 'sender_name', fallback='ZKTeco Attendance Bot').strip()


def set_smtp_sender_name(val: str):
    _ensure('smtp')
    _cfg['smtp']['sender_name'] = val.strip()
    _save()


def get_smtp_app_password() -> str:
    return _cfg.get('smtp', 'app_password', fallback='').strip()


def set_smtp_app_password(val: str):
    _ensure('smtp')
    _cfg['smtp']['app_password'] = val.strip()
    _save()


def get_smtp_recipients() -> list:
    raw = _cfg.get('smtp', 'recipients', fallback='').strip()
    return [r.strip() for r in raw.split(',') if r.strip()]


def set_smtp_recipients(recipients: list):
    _ensure('smtp')
    _cfg['smtp']['recipients'] = ','.join(recipients)
    _save()


def get_smtp_subject() -> str:
    return _cfg.get('smtp', 'subject',
                    fallback='Daily Absent Report - {date}').strip()


def set_smtp_subject(val: str):
    _ensure('smtp')
    _cfg['smtp']['subject'] = val.strip()
    _save()


def get_smtp_format() -> str:
    """'html' | 'plain' | 'both'"""
    return _cfg.get('smtp', 'format', fallback='html').strip().lower()


def set_smtp_format(val: str):
    _ensure('smtp')
    _cfg['smtp']['format'] = val.strip().lower()
    _save()
