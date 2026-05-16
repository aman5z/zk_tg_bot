"""
bot.py — ZKTeco Attendance Telegram Bot
Read-only from Middle East Attendance MDB + ZK device control.
No dashboard, no Flask, no SQLite. Telegram only.

Run: python bot.py
"""

import asyncio
import calendar as _cal
import configparser
import html
import ipaddress
import logging
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta
from io import BytesIO, StringIO

import pandas as pd
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                           MessageHandler, filters, ContextTypes)
from telegram.constants import ParseMode

import mdb_reader
import zk_devices
import notifier
import settings
import report_builder
import email_sender
from report_builder import TEMPLATES as REPORT_TEMPLATES, DEPT_ORDER
from settings import _DAY_NAMES

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger('ZKBot')

# ─── Config ───────────────────────────────────────────────────────────────────

_cfg = configparser.ConfigParser()
_cfg.read(os.path.join(os.path.dirname(__file__), 'config.ini'))

BOT_TOKEN   = _cfg.get('telegram', 'bot_token', fallback='').strip()
CHAT_ID     = _cfg.get('telegram', 'chat_id', fallback='').strip()
EXTRA_USERS = [u.strip() for u in
               _cfg.get('telegram', 'allowed_users', fallback='').split(',')
               if u.strip()]

os.makedirs(_cfg.get('reports', 'export_dir', fallback='/tmp/zk_reports'),
            exist_ok=True)

_SEC_SECTION = 'security'
SHELL_PASSWORD = _cfg.get(_SEC_SECTION, 'shell_password', fallback='').strip()
SHELL_ROOT_PASSWORD = _cfg.get(_SEC_SECTION, 'shell_root_password', fallback='').strip()
SHELL_SESSION_TIMEOUT_MINUTES = _cfg.getint(_SEC_SECTION, 'shell_session_timeout_minutes', fallback=5)
SHELL_MAX_FAILED_ATTEMPTS = max(1, _cfg.getint(_SEC_SECTION, 'shell_max_failed_attempts', fallback=3))
SHELL_LOCKOUT_MINUTES = max(1, _cfg.getint(_SEC_SECTION, 'shell_lockout_minutes', fallback=10))
SHELL_CMD_TIMEOUT_SECONDS = max(1, _cfg.getint(_SEC_SECTION, 'shell_cmd_timeout_seconds', fallback=8))
SHELL_BASE_DIR = os.path.realpath(_cfg.get(_SEC_SECTION, 'shell_base_dir', fallback='/tmp').strip() or '/tmp')
SHELL_ALLOWED_PATHS = [
    os.path.realpath(p.strip())
    for p in _cfg.get(_SEC_SECTION, 'shell_allowed_paths', fallback='/tmp,/var/log,/etc').split(',')
    if p.strip()
]
SQL_MAX_ROWS = max(1, _cfg.getint(_SEC_SECTION, 'sql_max_rows', fallback=50))
SQL_MAX_TEXT_CHARS = max(500, _cfg.getint(_SEC_SECTION, 'sql_max_text_chars', fallback=3200))
AUDIT_LOG_PATH = _cfg.get(_SEC_SECTION, 'audit_log_path', fallback='audit.log').strip() or 'audit.log'

_presence_state: dict = {}      # user_id -> {name, username, last_seen, last_activity}
_shell_auth_state: dict = {}    # chat_id -> pending auth state
_shell_sessions: dict = {}      # chat_id -> active shell session
_shell_lockouts: dict = {}      # user_id -> lockout metadata
_sql_prompt_state: dict = {}    # chat_id -> pending sql input metadata
_device_state: dict = {}        # chat_id -> pending /device prompt state
_bk_state: dict = {}            # chat_id -> pending /dbbackup prompt state

_audit_logger = logging.getLogger('ZKBot.Audit')
if not _audit_logger.handlers:
    _audit_handler = logging.FileHandler(AUDIT_LOG_PATH)
    _audit_handler.setFormatter(logging.Formatter(
        '%(asctime)s|%(levelname)s|%(message)s', '%Y-%m-%d %H:%M:%S'
    ))
    _audit_logger.addHandler(_audit_handler)
_audit_logger.setLevel(logging.INFO)
_audit_logger.propagate = False

# ─── Auth ─────────────────────────────────────────────────────────────────────

def _allowed(update: Update) -> bool:
    uid  = str(update.effective_user.id)
    cid  = str(update.effective_chat.id)
    allowed = cid == CHAT_ID or uid == CHAT_ID or uid in EXTRA_USERS
    if allowed:
        activity = ''
        msg = getattr(update, 'effective_message', None)
        if msg and msg.text:
            activity = msg.text.strip()[:120]
        cbq = getattr(update, 'callback_query', None)
        if not activity and cbq and cbq.data:
            activity = f'callback:{cbq.data[:80]}'
        _presence_state[uid] = {
            'name': update.effective_user.full_name,
            'username': update.effective_user.username or '',
            'last_seen': datetime.now(),
            'last_activity': activity or 'activity',
        }
    return allowed

async def _deny(update: Update):
    await update.message.reply_text('⛔ Unauthorized.')


def _safe_display_name(update: Update) -> str:
    user = update.effective_user
    if not user:
        return 'unknown'
    if user.username:
        return f'@{user.username}'
    return user.full_name or str(user.id)


def _audit(event: str, update: Update = None, detail: str = ''):
    if update and update.effective_user:
        uid = str(update.effective_user.id)
        name = _safe_display_name(update).replace('|', '/')
    else:
        uid = '-'
        name = '-'
    chat_id = str(update.effective_chat.id) if update and update.effective_chat else '-'
    cleaned = (detail or '').replace('\n', ' ').replace('|', '/')
    _audit_logger.info(f'event={event}|uid={uid}|user={name}|chat={chat_id}|detail={cleaned}')

# ─── Formatting helpers ───────────────────────────────────────────────────────

def _dept_bar(present: int, total: int) -> str:
    pct = round(present / total * 100) if total else 0
    icon = '🟢' if pct >= 90 else '🟡' if pct >= 70 else '🔴'
    return f"{icon} {present}/{total} ({pct}%)"

def _calendar_emoji(day: dict) -> str:
    if day['is_weekend']:
        return '⬛'
    return '🟩' if day['present'] else '🟥'

def _fmt_calendar(cal: dict) -> str:
    """Render monthly calendar as emoji grid."""
    days = cal['days']
    if not days:
        return 'No data.'

    # Header
    month_str = cal['month']
    lines = [f"📅 <b>{month_str}</b>  — Badge {cal['badge']}",
             f"✅ Present: {cal['present_days']}  ❌ Absent: {cal['absent_days']}  📆 Working: {cal['working_days']}",
             '',
             '<code>Mo Tu We Th Fr Sa Su</code>']

    # Pad first row
    first_wd = days[0]['date'].weekday()  # Mon=0
    week = ['⬜'] * first_wd

    for d in days:
        week.append(_calendar_emoji(d))
        if len(week) == 7:
            lines.append(' '.join(week))
            week = []
    if week:
        week += ['⬜'] * (7 - len(week))
        lines.append(' '.join(week))

    return '\n'.join(lines)

def _fmt_devices(statuses: list) -> str:
    lines = ['<b>📡 Device Status</b>\n']
    for s in statuses:
        icon = '🟢' if s['online'] else '🔴'
        line = f"{icon} <b>{s['name']}</b> ({s['ip']}:{s.get('port', 4370)})"
        if s['online']:
            extras = []
            if s.get('users'):
                extras.append(f"👤 {s['users']}")
            if s.get('time'):
                extras.append(f"🕐 {s['time']}")
            if extras:
                line += ' — ' + '  '.join(extras)
        else:
            line += f" — <i>{s.get('error','Unreachable')}</i>"
        lines.append(line)
    return '\n'.join(lines)


def _device_panel_text(statuses: list) -> str:
    lines = [
        '📟 <b>Device Admin Panel</b>',
        '',
        'Manage devices in <code>config.ini</code>. Changes apply live and are audit-logged.',
    ]
    if not statuses:
        lines.extend([
            '',
            'No devices configured yet.',
            'Tap <b>Add Device</b> below to create the first entry.',
        ])
        return '\n'.join(lines)
    for idx, s in enumerate(statuses, start=1):
        icon = '🟢' if s['online'] else '🔴'
        line = (
            f'\n<b>{idx}. {html.escape(s["name"])}</b> {icon}\n'
            f'IP: <code>{html.escape(s["ip"])}</code>\n'
            f'Port: <code>{html.escape(str(s.get("port", 4370)))}</code>'
        )
        extras = []
        if s.get('users') is not None:
            extras.append(f'👤 {s["users"]}')
        if s.get('time'):
            extras.append(f'🕐 {html.escape(s["time"])}')
        if extras:
            line += '\n' + '  '.join(extras)
        if not s['online'] and s.get('error'):
            line += f'\nStatus: <i>{html.escape(str(s["error"])[:120])}</i>'
        lines.append(line)
    return '\n'.join(lines)


def _device_panel_kb(statuses: list) -> InlineKeyboardMarkup:
    rows = []
    for idx, s in enumerate(statuses):
        rows.append([
            InlineKeyboardButton('✏️ Edit', callback_data=f'dev:edit:{idx}'),
            InlineKeyboardButton('❌ Remove', callback_data=f'dev:remove:{idx}'),
            InlineKeyboardButton('🏷 Rename', callback_data=f'dev:rename:{idx}'),
        ])
    rows.append([
        InlineKeyboardButton('➕ Add Device', callback_data='dev:add'),
        InlineKeyboardButton('🔄 Refresh', callback_data='dev:refresh'),
    ])
    return InlineKeyboardMarkup(rows)


def _device_action_kb(idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton('🌐 IP', callback_data=f'dev:edit_field:{idx}:ip'),
            InlineKeyboardButton('🏷 Name', callback_data=f'dev:edit_field:{idx}:name'),
            InlineKeyboardButton('🔌 Port', callback_data=f'dev:edit_field:{idx}:port'),
        ],
        [InlineKeyboardButton('← Back to /device', callback_data='dev:refresh')],
    ])


def _device_remove_kb(idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton('✅ Yes, remove', callback_data=f'dev:confirm_remove:{idx}'),
            InlineKeyboardButton('↩ Cancel', callback_data='dev:refresh'),
        ]
    ])


def _device_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('❌ Cancel', callback_data='dev:cancel')]
    ])

def _fmt_today(summary: dict, mode: str = 'full') -> str:
    d = summary
    dept_lines = []
    for dept, s in sorted(d['dept_stats'].items()):
        total = s['present'] + s['absent']
        dept_lines.append(f"  {_dept_bar(s['present'], total)} {dept}")

    if mode == 'absent':
        names = '\n'.join(f"  ❌ {e['name']} ({e['dept']})"
                          for e in sorted(d['absent'], key=lambda x: x['dept']))
        return (f"❌ <b>Absent — {d['date']}</b>\n"
                f"Total: {d['absent_count']}\n\n{names or 'None'}")

    if mode == 'present':
        names = '\n'.join(f"  ✅ {e['name']} ({e['dept']})"
                          for e in sorted(d['present'], key=lambda x: x['dept']))
        return (f"✅ <b>Present — {d['date']}</b>\n"
                f"Total: {d['present_count']}\n\n{names or 'None'}")

    return (
        f"📊 <b>Today — {d['date']}</b>\n\n"
        f"👥 Total: {d['total']}  ✅ {d['present_count']}  ❌ {d['absent_count']}\n\n"
        f"<b>By Department:</b>\n" + '\n'.join(dept_lines)
    )

# ─── Interactive calendar state (per chat) ────────────────────────────────────

_cal_state: dict = {}   # chat_id → {'badge', 'name', 'dept', ['range_from']}
_importcsv_state: dict = {}  # chat_id → awaiting CSV upload (read-only preview)

# ─── Interactive calendar keyboard builder ────────────────────────────────────

def _make_cal_keyboard(year: int, month: int, mode: str,
                       badge: str, range_from: str = '') -> InlineKeyboardMarkup:
    """
    Build a month-grid InlineKeyboardMarkup.
    mode: 'S'=single date, 'F'=range start, 'T'=range end
    """
    prev_y, prev_m = (year, month - 1) if month > 1 else (year - 1, 12)
    next_y, next_m = (year, month + 1) if month < 12 else (year + 1, 1)
    rf = range_from

    def nav_cb(y, m):
        return f'cal_nav:{y}:{m}:{mode}:{badge}:{rf}'

    month_label = datetime(year, month, 1).strftime('%b %Y')

    rows = [
        [
            InlineKeyboardButton('◀', callback_data=nav_cb(prev_y, prev_m)),
            InlineKeyboardButton(month_label, callback_data='cal_noop'),
            InlineKeyboardButton('▶', callback_data=nav_cb(next_y, next_m)),
        ],
        [InlineKeyboardButton(d, callback_data='cal_noop')
         for d in ['Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su']],
    ]

    first_col  = datetime(year, month, 1).weekday()
    total_days = _cal.monthrange(year, month)[1]
    week = [InlineKeyboardButton(' ', callback_data='cal_noop')] * first_col

    for day in range(1, total_days + 1):
        date_str = f'{year:04d}-{month:02d}-{day:02d}'
        week.append(InlineKeyboardButton(
            str(day),
            callback_data=f'cal_day:{date_str}:{mode}:{badge}:{rf}',
        ))
        if len(week) == 7:
            rows.append(week)
            week = []

    if week:
        week += [InlineKeyboardButton(' ', callback_data='cal_noop')] * (7 - len(week))
        rows.append(week)

    rows.append([InlineKeyboardButton('❌ Cancel', callback_data='att_cancel')])
    return InlineKeyboardMarkup(rows)

# ─── Interactive calendar result formatters ───────────────────────────────────

def _fmt_day_punches(badge: str, name: str, dept: str, date_str: str) -> str:
    """Format punch records for a single day."""
    try:
        d = datetime.strptime(date_str, '%Y-%m-%d').date()
        punches = mdb_reader.get_employee_punches(badge, d, d)
        day_label = d.strftime('%A %d %b %Y')
    except Exception as exc:
        return f'❌ Error fetching punches: {str(exc)[:100]}'

    lines = [
        f'👤 <b>{name}</b> ({badge}) — {dept}',
        f'📅 {day_label}',
        '',
    ]
    if not punches:
        lines.append('❌ No punches recorded.')
    else:
        lines.append(f'✅ <b>{len(punches)} punch(es):</b>')
        for p in punches:
            dev = p.get('device') or '?'
            lines.append(f"  🕐 {p['time']}  (<code>{dev}</code>)")
        lines.append('')
        if len(punches) >= 2:
            lines.append(f"⏩ First: {punches[0]['time']}   Last: {punches[-1]['time']}")
    return '\n'.join(lines)

def _fmt_range_punches(badge: str, name: str, dept: str,
                       d_from_str: str, d_to_str: str) -> str:
    """Format punch records for a date range."""
    try:
        d_from  = datetime.strptime(d_from_str, '%Y-%m-%d').date()
        d_to    = datetime.strptime(d_to_str,   '%Y-%m-%d').date()
        punches = mdb_reader.get_employee_punches(badge, d_from, d_to)
    except Exception as exc:
        return f'❌ Error fetching punches: {str(exc)[:100]}'

    total_days = (d_to - d_from).days + 1
    by_date: dict = {}
    for p in punches:
        key = p['date'].strftime('%Y-%m-%d')
        by_date.setdefault(key, []).append(p)

    header = [
        f'👤 <b>{name}</b> ({badge}) — {dept}',
        f'📅 {d_from.strftime("%d %b")} – {d_to.strftime("%d %b %Y")}  ({total_days} days)',
        '',
    ]

    present = absent = weekend = 0
    lines   = []
    compact = total_days > 14
    d = d_from
    while d <= d_to:
        dow        = d.weekday()
        day_key    = d.strftime('%Y-%m-%d')
        day_recs   = by_date.get(day_key, [])
        is_weekend = dow in (4, 5)  # Fri/Sat = weekend in Middle East

        if is_weekend:
            weekend += 1
        else:
            if day_recs:
                present += 1
            else:
                absent += 1

        if compact:
            if day_recs:
                t_in  = day_recs[0]['time']
                t_out = day_recs[-1]['time'] if len(day_recs) > 1 else '—     '
                lines.append(f"{d.strftime('%d %b %a')}  {t_in}  {t_out}  ({len(day_recs)})")
            elif not is_weekend:
                lines.append(f"{d.strftime('%d %b %a')}  —         —         0")
        else:
            day_str = d.strftime('%a %d %b')
            if is_weekend:
                lines.append(f'📆 {day_str}  🏖 Weekend')
            elif not day_recs:
                lines.append(f'📆 {day_str}  ❌  No punches')
            elif len(day_recs) == 1:
                lines.append(f"📆 {day_str}  ✅  IN {day_recs[0]['time']}  (1 punch — no out)")
            else:
                lines.append(f"📆 {day_str}  ✅  IN {day_recs[0]['time']}"
                             f"  OUT {day_recs[-1]['time']}  ({len(day_recs)} punches)")
        d += timedelta(days=1)

    summary = f'\nSummary: {present} present, {absent} absent'
    if weekend:
        summary += f', {weekend} weekend'
    return '\n'.join(header + lines + [summary])

# ─── Handlers ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    await update.message.reply_text(
        "👋 <b>ZKTeco Attendance Bot</b>\n\nSend /help for commands.",
        parse_mode=ParseMode.HTML)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    text = (
        "📋 <b>Available Commands</b>\n\n"
        "<b>Attendance</b>\n"
        "/today — present/absent + dept breakdown\n"
        "/absent — absent list only\n"
        "/present — present list only\n"
        "/late — late arrivals today\n"
        "/early — early arrivals today\n"
        "/whoisin — currently inside building\n"
        "/feed — last 20 punches\n"
        "/latest — device status + last 2 MDB punches per device\n"
        "/week — this week summary\n"
        "/month — monthly dept summary\n"
        "/topabsent — most absent this month\n"
        "/dept &lt;name&gt; — today's attendance by department\n"
        "/employeereport &lt;badge&gt; — employee month-to-date report\n"
        "/history &lt;DD/MM/YYYY&gt; &lt;DD/MM/YYYY&gt; — range report\n"
        "/syncrange &lt;DD/MM/YYYY&gt; &lt;DD/MM/YYYY&gt; — read-only range summary\n"
        "/trend — attendance trend (last 14 working days)\n"
        "/report — send absent report (XLSX/PNG/PDF)\n\n"
        "<b>Employee</b>\n"
        "/search &lt;name or badge&gt; — find employee\n"
        "/punches &lt;badge&gt; — today's punches\n"
        "/calendar &lt;badge&gt; — interactive date/range picker\n"
        "/calendar &lt;badge&gt; YYYY-MM — static monthly calendar\n\n"
        "<b>Devices</b>\n"
        "/device — device admin panel (status/add/edit/remove/rename)\n"
        "/devices — all device status\n"
        "/clocksync — sync all device clocks\n"
        "/reboot &lt;ip or name&gt; | all — reboot a device or all\n"
        "/usersync — sync users across devices\n"
        "/adduser &lt;badge&gt; &lt;name&gt; — add user to all devices\n"
        "/unknown — users on devices not in MDB\n\n"
        "<b>Settings</b>\n"
        "/livepunches — toggle live punch notifications on/off\n"
        "/editreport — configure on-demand /report settings\n"
        "/editdaily — configure scheduled daily report settings\n"
        "/editemail — configure Gmail SMTP email delivery, send time, and days (optional)\n"
        "/mail — send attendance report via email (today or pick date)\n\n"
        "<b>Admin & Security</b>\n"
        "/admin — admin control panel\n"
        "/shell — open protected limited shell session\n"
        "/su — elevate current shell session (requires root password)\n"
        "/sql &lt;SELECT ...&gt; — run read-only SQL query (SELECT only)\n"
        "/auditlog [N|YYYY-MM-DD] — view audit entries\n"
        "/presence or /status — show authorized users last activity\n"
        "/exit — end active shell/sql prompt session\n\n"
        "<b>Database</b>\n"
        "/stats — MDB stats\n"
        "/mdbinfo — MDB path + file info\n"
        "/setmdb &lt;path&gt; — update MDB path\n"
        "/tables — list MDB tables (diagnostics)\n"
        "/download &lt;ip&gt; — read-only device snapshot (no MDB write)\n"
        "/dbbackup — send a read-only MDB file copy\n"
        "/importcsv — upload CSV for validation/preview only\n"
        "/autonmap — suggest UID→badge matches only (not saved)\n"
        "/shifts — read-only shift configuration view\n"
        "/workdays — read-only workday configuration view\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

def _admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton('🖥 Shell', callback_data='adm:shell'),
            InlineKeyboardButton('🧮 SQL', callback_data='adm:sql'),
        ],
        [
            InlineKeyboardButton('📜 Audit', callback_data='adm:audit'),
            InlineKeyboardButton('👥 Presence', callback_data='adm:presence'),
        ],
        [
            InlineKeyboardButton('⚙️ Config', callback_data='adm:config'),
            InlineKeyboardButton('👤 Users', callback_data='adm:users'),
        ],
        [
            InlineKeyboardButton('📟 Device', callback_data='adm:device'),
            InlineKeyboardButton('📢 Notice', callback_data='adm:notice'),
        ],
    ])


def _presence_lines() -> list:
    lines = ['👥 <b>User Presence</b>\n']
    now = datetime.now()
    if not _presence_state:
        lines.append('No activity recorded yet.')
        return lines
    ordered = sorted(_presence_state.values(), key=lambda x: x['last_seen'], reverse=True)
    for st in ordered:
        delta = now - st['last_seen']
        mins = max(0, int(delta.total_seconds() // 60))
        ago = f'{mins}m ago'
        if mins >= 60:
            ago = f'{mins // 60}h {mins % 60}m ago'
        user = html.escape(st['name'])
        if st.get('username'):
            user += f" (@{html.escape(st['username'])})"
        activity = html.escape(st.get('last_activity') or 'activity')
        lines.append(f"• {user}\n  Last seen: {st['last_seen'].strftime('%Y-%m-%d %H:%M:%S')} ({ago})\n  Activity: <code>{activity[:80]}</code>")
    return lines


def _read_audit_lines(limit: int = 20, day: str = '') -> list:
    if not os.path.isfile(AUDIT_LOG_PATH):
        return ['No audit log file yet.']
    try:
        with open(AUDIT_LOG_PATH, 'r', encoding='utf-8', errors='replace') as fh:
            lines = [ln.strip() for ln in fh if ln.strip()]
    except OSError as exc:
        return [f'Failed to read audit log: {exc}']
    if day:
        lines = [ln for ln in lines if ln.startswith(day)]
    return lines[-limit:] if limit > 0 else lines


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    _audit('admin.open', update, 'opened /admin panel')
    await update.message.reply_text(
        '🛠 <b>Admin Panel</b>\nSelect an action:',
        parse_mode=ParseMode.HTML,
        reply_markup=_admin_menu_kb()
    )


async def cmd_presence(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    lines = _presence_lines()
    for chunk in _split('\n'.join(lines)):
        await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)


async def cmd_auditlog(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    limit = 20
    day = ''
    if ctx.args:
        arg = ctx.args[0].strip()
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', arg):
            day = arg
        elif arg.isdigit():
            limit = max(1, min(int(arg), 200))
        else:
            await update.message.reply_text('Usage: /auditlog [N|YYYY-MM-DD]')
            return
    lines = _read_audit_lines(limit=limit, day=day)
    _audit('auditlog.view', update, f'limit={limit} day={day or "-"} results={len(lines)}')
    header = f'📜 <b>Audit Log</b> ({len(lines)} entr{"y" if len(lines) == 1 else "ies"})'
    text = header + '\n\n' + '\n'.join(html.escape(ln) for ln in lines)
    for chunk in _split(text):
        await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)


async def cmd_shell(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    user_id = str(update.effective_user.id)
    chat_id = str(update.effective_chat.id)
    locked, mins = _is_locked_out(user_id)
    if locked:
        _audit('shell.locked', update, f'locked={mins}m')
        await update.message.reply_text(f'⛔ Shell access locked. Try again in {mins} minute(s).')
        return
    if not SHELL_PASSWORD:
        _audit('shell.denied', update, 'shell password not configured')
        await update.message.reply_text('❌ Shell is not configured. Set [security] shell_password in config.ini.')
        return
    _shell_auth_state[chat_id] = {
        'user_id': user_id,
        'stage': 'shell',
        'expires_at': datetime.now() + timedelta(minutes=2),
    }
    _audit('shell.auth.prompt', update, 'requested shell auth')
    await update.message.reply_text(
        '🔒 Enter shell session password.\n'
        'Session unlock expires in 2 minutes.',
    )


async def cmd_su(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    sess = _get_active_shell(chat_id)
    if not sess or sess.get('user_id') != user_id:
        await update.message.reply_text('❌ No active shell session for you. Start with /shell.')
        return
    if not SHELL_ROOT_PASSWORD:
        await update.message.reply_text('❌ Root escalation is not configured.')
        return
    _shell_auth_state[chat_id] = {
        'user_id': user_id,
        'stage': 'su',
        'expires_at': datetime.now() + timedelta(minutes=2),
    }
    _audit('shell.su.prompt', update, 'requested su password')
    await update.message.reply_text('🔒 Enter root shell password to elevate this session.')


async def cmd_exit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    sess = _shell_sessions.get(chat_id)
    if sess and sess.get('user_id') == user_id:
        _shell_sessions.pop(chat_id, None)
        _shell_auth_state.pop(chat_id, None)
        _audit('shell.end', update, 'session ended by /exit')
        await update.message.reply_text('✅ Shell session ended.')
        return
    prompt = _sql_prompt_state.get(chat_id)
    if prompt and prompt.get('user_id') == user_id:
        _sql_prompt_state.pop(chat_id, None)
        _audit('sql.prompt.end', update, 'sql prompt ended by /exit')
        await update.message.reply_text('✅ SQL prompt ended.')
        return
    await update.message.reply_text('ℹ️ No active shell/sql prompt session to exit.')


async def cmd_sql(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    query = ' '.join(ctx.args).strip()
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    if not query:
        _sql_prompt_state[chat_id] = {
            'user_id': user_id,
            'expires_at': datetime.now() + timedelta(minutes=5),
        }
        await update.message.reply_text(
            '🧮 SQL read-only console ready for 5 minutes.\n'
            'Send a SELECT query as plain text.\n'
            'Only simple SELECT ... FROM ... [WHERE col=value AND ...] [LIMIT N].\n'
            'Send /exit to close.'
        )
        _audit('sql.prompt.start', update, 'started SQL prompt session')
        return
    ok, result, csv_buf = _execute_readonly_sql(query)
    _audit('sql.query', update, f'ok={ok} query={query[:180]}')
    if not ok:
        await update.message.reply_text(f'❌ {result}')
        return
    await update.message.reply_text(f'✅ <b>SQL Result</b>\n<code>{html.escape(result)}</code>', parse_mode=ParseMode.HTML)
    if csv_buf:
        csv_buf.seek(0)
        await update.message.reply_document(
            document=csv_buf,
            filename=f"sql_result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            caption='Read-only SQL result (CSV)'
        )


async def callback_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return
    data = query.data or ''
    action = data.split(':', 1)[1] if ':' in data else ''
    if action == 'shell':
        await query.edit_message_text('🖥 Use /shell to start a protected limited shell session.')
        return
    if action == 'sql':
        await query.edit_message_text('🧮 Use /sql <SELECT ...> or /sql to open prompt mode.')
        return
    if action == 'audit':
        await query.edit_message_text('📜 Use /auditlog 20 or /auditlog YYYY-MM-DD.')
        return
    if action == 'presence':
        await query.edit_message_text('\n'.join(_presence_lines()), parse_mode=ParseMode.HTML)
        return
    if action == 'config':
        shell_set = '✅' if bool(SHELL_PASSWORD) else '❌'
        root_set = '✅' if bool(SHELL_ROOT_PASSWORD) else '❌'
        text = (
            '⚙️ <b>Security Config Status</b>\n\n'
            f'Shell password configured: {shell_set}\n'
            f'Root shell password configured: {root_set}\n'
            f'Session timeout: {SHELL_SESSION_TIMEOUT_MINUTES} min\n'
            f'Lockout: {SHELL_MAX_FAILED_ATTEMPTS} attempts / {SHELL_LOCKOUT_MINUTES} min'
        )
        await query.edit_message_text(text, parse_mode=ParseMode.HTML)
        return
    if action == 'users':
        all_users = [CHAT_ID] + EXTRA_USERS
        text = '👤 <b>Authorized Users</b>\n\n' + '\n'.join(f'• <code>{html.escape(str(u))}</code>' for u in all_users if str(u).strip())
        await query.edit_message_text(text, parse_mode=ParseMode.HTML)
        return
    if action == 'device':
        await query.edit_message_text('📟 Use /device to open the device admin panel.')
        return
    if action == 'notice':
        st = _edit_state.setdefault(str(update.effective_chat.id), {})
        st['ctx'] = 'admin'
        st['awaiting'] = 'admin_notice'
        await query.edit_message_text(
            '📢 Send notice text in your next message.\n'
            'It will be posted to this authorized chat.'
        )
        return
    await query.edit_message_text('Unknown admin action.')

# ── Attendance commands ──────────────────────────────────────────────────────

async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    await update.message.reply_text('⏳ Fetching...')
    try:
        summary = mdb_reader.get_today_summary()
        await update.message.reply_text(_fmt_today(summary, 'full'),
                                        parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def cmd_absent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    await update.message.reply_text('⏳ Fetching...')
    try:
        summary = mdb_reader.get_today_summary()
        text = _fmt_today(summary, 'absent')
        # Telegram 4096 char limit — split if needed
        for chunk in _split(text):
            await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def cmd_present(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    await update.message.reply_text('⏳ Fetching...')
    try:
        summary = mdb_reader.get_today_summary()
        text = _fmt_today(summary, 'present')
        for chunk in _split(text):
            await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def cmd_late(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    await update.message.reply_text('⏳ Checking...')
    try:
        late = mdb_reader.get_late_today()
        if not late:
            await update.message.reply_text('✅ No late arrivals today.')
            return
        lines = [f"⏰ <b>Late Arrivals — {date.today().strftime('%d/%m/%Y')}</b>\n"]
        for e in late:
            lines.append(f"🔴 {e['name']} ({e['dept']})\n"
                         f"   Punched: {e['punch_time']} | Late: {e['late_mins']}m")
        await update.message.reply_text('\n'.join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def cmd_early(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    await update.message.reply_text('⏳ Checking...')
    try:
        early = mdb_reader.get_early_today()
        if not early:
            await update.message.reply_text('✅ No early arrivals before shift start today.')
            return
        lines = [f"🌅 <b>Early Arrivals — {date.today().strftime('%d/%m/%Y')}</b>\n"]
        for e in early:
            lines.append(f"🟢 {e['name']} ({e['dept']})\n"
                         f"   Punched: {e['punch_time']} | Early: {e['early_mins']}m")
        for chunk in _split('\n'.join(lines)):
            await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def cmd_dept(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage: /dept <name>"""
    if not _allowed(update):
        return await _deny(update)
    if not ctx.args:
        await update.message.reply_text(
            'Usage: /dept &lt;name&gt;\nExample: /dept Admin',
            parse_mode=ParseMode.HTML)
        return
    query = ' '.join(ctx.args).strip()
    await update.message.reply_text(f'⏳ Looking up department "{query}"...')
    try:
        result = mdb_reader.get_department_today(query)
        matches = result.get('matches', [])
        if not matches:
            await update.message.reply_text(f'❌ No matching department found for "{query}".')
            return
        lines = [f"🏢 <b>Department Attendance — {result.get('date', '')}</b>\n"]
        for item in matches:
            total = item['present_count'] + item['absent_count']
            lines.append(
                f"\n<b>{item['dept']}</b>\n"
                f"✅ Present: {item['present_count']}  ❌ Absent: {item['absent_count']}  👥 Total: {total}"
            )
            if item['absent']:
                lines.append("Absent list:")
                for emp in item['absent'][:30]:
                    lines.append(f"  • {emp['name']} ({emp['badge']})")
                if len(item['absent']) > 30:
                    lines.append(f"  …and {len(item['absent']) - 30} more")
        for chunk in _split('\n'.join(lines)):
            await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def cmd_whoisin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    await update.message.reply_text('⏳ Checking...')
    try:
        inside = mdb_reader.get_who_is_in()
        if not inside:
            await update.message.reply_text('🏫 No one currently inside (or all have checked out).')
            return
        lines = [f"🏢 <b>Currently Inside — {len(inside)} people</b>\n"]
        for e in sorted(inside, key=lambda x: x['dept']):
            lines.append(f"✅ {e['name']} ({e['dept']}) — last punch {e['last_punch']}")
        for chunk in _split('\n'.join(lines)):
            await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def cmd_feed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    try:
        feed = mdb_reader.get_punch_feed(20)
        if not feed:
            await update.message.reply_text('No punches today yet.')
            return
        lines = [f"📡 <b>Last {len(feed)} Punches Today</b>\n"]
        for p in feed:
            lines.append(f"🕐 {p['time']}  {p['name']} ({p['dept']})")
        await update.message.reply_text('\n'.join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    await update.message.reply_text('⏳ Fetching week data...')
    try:
        history = mdb_reader.get_week_summary()
        lines = ['📅 <b>This Week</b>\n']
        for day in history:
            wd = day['weekday'][:3]
            ds = day['date_str']
            if day['is_weekend']:
                lines.append(f"⬛ {wd} {ds} — Weekend")
            else:
                total = day['present_count'] + day['absent_count']
                lines.append(
                    f"{'🟢' if day['absent_count'] == 0 else '🟡'} "
                    f"{wd} {ds} — ✅{day['present_count']} ❌{day['absent_count']} "
                    f"/{total}")
        await update.message.reply_text('\n'.join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def cmd_month(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    await update.message.reply_text('⏳ Fetching month data...')
    try:
        dept_summary = mdb_reader.get_month_dept_summary()
        today = date.today()
        lines = [f"📊 <b>Month to Date — {today.strftime('%B %Y')}</b>\n"]
        for s in dept_summary:
            icon = '🟢' if s['percent'] >= 90 else '🟡' if s['percent'] >= 70 else '🔴'
            lines.append(f"{icon} {s['dept']}: {s['present']}/{s['total']} ({s['percent']}%)")
        await update.message.reply_text('\n'.join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def cmd_topabsent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    await update.message.reply_text('⏳ Calculating...')
    try:
        top = mdb_reader.get_top_absent(10)
        today = date.today()
        lines = [f"🔴 <b>Top Absent — {today.strftime('%B %Y')}</b>\n"]
        for i, e in enumerate(top, 1):
            lines.append(
                f"{i}. {e['name']} ({e['dept']})\n"
                f"   Absent: {e['absent_days']}d / Working: {e['working_days']}d")
        await update.message.reply_text('\n'.join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage: /history DD/MM/YYYY DD/MM/YYYY"""
    if not _allowed(update):
        return await _deny(update)
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text(
            '📖 Usage: /history DD/MM/YYYY DD/MM/YYYY\n'
            'Example: /history 01/05/2026 13/05/2026')
        return
    try:
        d_from = datetime.strptime(args[0], '%d/%m/%Y').date()
        d_to   = datetime.strptime(args[1], '%d/%m/%Y').date()
    except ValueError:
        await update.message.reply_text('❌ Date format: DD/MM/YYYY')
        return
    if (d_to - d_from).days > 31:
        await update.message.reply_text('❌ Max range is 31 days.')
        return
    await update.message.reply_text(f'⏳ Fetching {args[0]} → {args[1]}...')
    try:
        history = mdb_reader.get_history(d_from, d_to)
        lines = [f"📅 <b>History: {args[0]} → {args[1]}</b>\n"]
        for day in history:
            wd = day['weekday'][:3]
            if day['is_weekend']:
                lines.append(f"⬛ {wd} {day['date_str']} — Weekend")
            else:
                total = day['present_count'] + day['absent_count']
                lines.append(
                    f"{'🟢' if day['absent_count'] == 0 else '🟡'} "
                    f"{wd} {day['date_str']} — ✅{day['present_count']} ❌{day['absent_count']} /{total}")
        for chunk in _split('\n'.join(lines)):
            await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def cmd_syncrange(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage: /syncrange DD/MM/YYYY DD/MM/YYYY (read-only summary)."""
    if not _allowed(update):
        return await _deny(update)
    if len(ctx.args) < 2:
        await update.message.reply_text(
            'Usage: /syncrange DD/MM/YYYY DD/MM/YYYY\n'
            'Example: /syncrange 01/05/2026 13/05/2026')
        return
    try:
        d_from = datetime.strptime(ctx.args[0], '%d/%m/%Y').date()
        d_to = datetime.strptime(ctx.args[1], '%d/%m/%Y').date()
    except ValueError:
        await update.message.reply_text('❌ Date format: DD/MM/YYYY')
        return
    if (d_to - d_from).days > 31:
        await update.message.reply_text('❌ Max range is 31 days.')
        return
    await update.message.reply_text(
        f'⏳ Read-only sync preview for {ctx.args[0]} → {ctx.args[1]}...')
    try:
        data = mdb_reader.get_sync_range_summary(d_from, d_to)
        lines = [
            f"🔎 <b>Sync Range (Read-Only) — {ctx.args[0]} → {ctx.args[1]}</b>",
            "No writes were made to MDB or devices.",
            f"📈 Working-day average present: {data['average_present_pct']}%",
            "",
        ]
        for row in data['rows']:
            if row['is_weekend']:
                lines.append(f"⬛ {row['weekday'][:3]} {row['date_str']} — Weekend")
            else:
                lines.append(
                    f"{'🟢' if row['absent_count'] == 0 else '🟡'} "
                    f"{row['weekday'][:3]} {row['date_str']} — "
                    f"✅{row['present_count']} ❌{row['absent_count']} ({row['present_pct']}%)"
                )
        for chunk in _split('\n'.join(lines)):
            await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def cmd_trend(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage: /trend [working_days]"""
    if not _allowed(update):
        return await _deny(update)
    days = 14
    if ctx.args:
        try:
            days = int(ctx.args[0])
        except ValueError:
            await update.message.reply_text('❌ Usage: /trend [working_days]')
            return
    await update.message.reply_text('⏳ Building trend...')
    try:
        trend = mdb_reader.get_attendance_trend(days)
        if not trend:
            await update.message.reply_text('No trend data available.')
            return
        lines = [f"📈 <b>Attendance Trend — Last {len(trend)} Working Days</b>\n"]
        prev = None
        for row in trend:
            arrow = '⏺'
            if prev is not None:
                if row['present_pct'] > prev:
                    arrow = '⬆️'
                elif row['present_pct'] < prev:
                    arrow = '⬇️'
                else:
                    arrow = '➡️'
            lines.append(
                f"{arrow} {row['date_str']}: {row['present_pct']}% "
                f"(✅{row['present_count']} / ❌{row['absent_count']})"
            )
            prev = row['present_pct']
        for chunk in _split('\n'.join(lines)):
            await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show date-selection prompt for the absent report."""
    if not _allowed(update):
        return await _deny(update)
    today     = date.today()
    yesterday = today - timedelta(days=1)
    keyboard  = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f'📅 Today ({today.strftime("%d/%m/%Y")})',
                callback_data='rep:today',
            ),
            InlineKeyboardButton(
                f'📅 Yesterday ({yesterday.strftime("%d/%m/%Y")})',
                callback_data='rep:yesterday',
            ),
        ],
        [
            InlineKeyboardButton('📆 Pick Date',   callback_data='rep:pick'),
            InlineKeyboardButton('📆 Date Range',  callback_data='rep:range'),
        ],
        [InlineKeyboardButton('❌ Cancel',     callback_data='rep:cancel')],
    ])
    await update.message.reply_text(
        '📊 <b>Absent Report</b>\n\nSelect date:',
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


def _save_report_files_to_dir(files: dict, save_dir: str):
    """Save report BytesIO buffers to *save_dir*. Logs errors but does not raise."""
    try:
        os.makedirs(save_dir, exist_ok=True)
        for fmt_key, (buf, filename) in files.items():
            dest = os.path.join(save_dir, filename)
            buf.seek(0)
            with open(dest, 'wb') as fh:
                fh.write(buf.read())
            logger.info(f"Report saved to {dest}")
    except OSError as e:
        logger.error(f"Failed to save report to {save_dir}: {e}")


async def _send_absent_report_callback(query: CallbackQuery, update: Update, report_date: date):
    """Build and send the absent report for *report_date* via a callback context."""
    try:
        history = mdb_reader.get_history(report_date, report_date)
        if not history:
            await query.edit_message_text(
                f'❌ No data for {report_date.strftime("%d/%m/%Y")}')
            return
        day_data = history[0]
        absent   = day_data['absent']

        if not absent:
            await query.edit_message_text(
                f'✅ No absences on {report_date.strftime("%d/%m/%Y")}. All staff present!')
            return

        depts    = settings.get_report_departments()
        formats  = settings.get_report_formats()
        template = settings.get_report_template()

        files = report_builder.build_absent_report(
            absent=absent,
            report_date=report_date,
            departments=depts,
            formats=formats,
            template=template,
        )
        caption = (f'Absent list — {report_date.strftime("%d/%m/%Y")}'
                   f'  ({len(absent)} absent)')
        for fmt_key, (buf, filename) in files.items():
            buf.seek(0)
            if fmt_key == 'png':
                await update.effective_chat.send_photo(photo=buf, caption=caption)
            else:
                await update.effective_chat.send_document(
                    document=buf, filename=filename, caption=caption)

        # Save report files to configured save directory
        save_dir = settings.get_report_save_dir()
        if save_dir:
            _save_report_files_to_dir(files, save_dir)

        await query.edit_message_text(
            f'✅ Report sent for {report_date.strftime("%d/%m/%Y")}')
    except Exception as e:
        await query.edit_message_text(f'❌ {e}')

async def _send_range_absent_reports_callback(
    query: CallbackQuery, update: Update, d_from_str: str, d_to_str: str
):
    """Build and send absent reports for every date in [d_from_str, d_to_str]."""
    try:
        d_from = datetime.strptime(d_from_str, '%Y-%m-%d').date()
        d_to   = datetime.strptime(d_to_str,   '%Y-%m-%d').date()
    except ValueError:
        await query.edit_message_text('❌ Invalid date range.')
        return

    total_days = (d_to - d_from).days + 1
    if total_days > 31:
        await query.edit_message_text(
            f'❌ Date range too large ({total_days} days). Please select at most 31 days.')
        return

    await query.edit_message_text(
        f'⏳ Building reports for {d_from.strftime("%d/%m/%Y")} – {d_to.strftime("%d/%m/%Y")} '
        f'({total_days} days)…')

    depts    = settings.get_report_departments()
    formats  = settings.get_report_formats()
    template = settings.get_report_template()
    save_dir = settings.get_report_save_dir()

    sent   = 0
    errors = []
    current = d_from
    while current <= d_to:
        try:
            history = mdb_reader.get_history(current, current)
            if not history:
                current += timedelta(days=1)
                continue
            absent = history[0]['absent']
            if not absent:
                current += timedelta(days=1)
                continue

            files = report_builder.build_absent_report(
                absent=absent,
                report_date=current,
                departments=depts,
                formats=formats,
                template=template,
            )
            caption = (f'Absent list — {current.strftime("%d/%m/%Y")}'
                       f'  ({len(absent)} absent)')
            for fmt_key, (buf, filename) in files.items():
                buf.seek(0)
                if fmt_key == 'png':
                    await update.effective_chat.send_photo(photo=buf, caption=caption)
                else:
                    await update.effective_chat.send_document(
                        document=buf, filename=filename, caption=caption)

            if save_dir:
                _save_report_files_to_dir(files, save_dir)

            sent += 1
        except Exception as e:
            errors.append(f'{current.strftime("%d/%m/%Y")}: {e}')

        current += timedelta(days=1)

    summary = (f'✅ Range report done: {d_from.strftime("%d/%m/%Y")} – {d_to.strftime("%d/%m/%Y")}\n'
               f'📄 Reports sent: {sent}')
    if errors:
        summary += '\n⚠️ Errors:\n' + '\n'.join(errors[:5])
    await update.effective_chat.send_message(summary)


# ── /latest — latest MDB punches + live device times ─────────────────────────

async def cmd_latest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Ping all devices, then show 2 most recent MDB punches per online device
    (grouped by SENSORID).  4 online devices → up to 8 punch entries.
    """
    if not _allowed(update):
        return await _deny(update)
    await update.message.reply_text('⏳ Pinging devices and fetching MDB…')
    lines = ['🔄 <b>Latest MDB Activity</b>\n']

    # ── 1. Ping all devices ──────────────────────────────────────────────
    device_statuses = []
    try:
        device_statuses = zk_devices.get_device_status()
        online_count = sum(1 for s in device_statuses if s['online'])
        lines.append(
            f'📡 <b>Devices: {online_count}/{len(device_statuses)} online</b>'
        )
        for s in device_statuses:
            icon  = '🟢' if s['online'] else '🔴'
            extra = f" — 🕐 {s['time']}" if s['online'] and s.get('time') else ''
            lines.append(f"  {icon} {s['name']} ({s['ip']}){extra}")
    except Exception as e:
        lines.append(f'  ⚠️ Device ping failed: {e}')

    # ── 2. MDB file modification time ────────────────────────────────────
    try:
        info = mdb_reader.get_mdb_info()
        if info.get('last_modified'):
            lines.append(f"\n📁 <b>MDB Last Modified:</b> {info['last_modified']}")
    except Exception:
        pass

    # ── 3. Latest 2 punches per device SENSORID ──────────────────────────
    lines.append('\n📌 <b>Latest 2 Punches per Device (MDB):</b>')
    try:
        per_device = mdb_reader.get_latest_punches_per_device(n=2, days_back=3)
        if per_device:
            for sensor_id, punches in sorted(per_device.items(),
                                             key=lambda kv: kv[0]):
                lines.append(f"\n  🖥 <b>Sensor / Device ID: {sensor_id}</b>")
                for p in punches:
                    date_label = (p['date'].strftime('%d/%m/%Y')
                                  if p['date'] != date.today() else 'Today')
                    lines.append(
                        f"    🕐 {date_label} {p['time']}  "
                        f"👤 {p['name']}  🏷 {p['badge']}  🏢 {p['dept']}"
                    )
        else:
            lines.append('  No recent punches found in MDB.')
    except Exception as e:
        lines.append(f'  ⚠️ Punch fetch error: {e}')

    for chunk in _split('\n'.join(lines)):
        await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)


# ── /livepunches — toggle live punch notifications ────────────────────────────

async def cmd_livepunches(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Toggle live punch notifications on or off."""
    if not _allowed(update):
        return await _deny(update)
    current = settings.get_live_punches()
    new_val = not current
    settings.set_live_punches(new_val)
    icon   = '🟢' if new_val else '🔴'
    state  = 'ENABLED' if new_val else 'DISABLED'
    await update.message.reply_text(
        f"{icon} <b>Live Punch Notifications {state}</b>\n\n"
        f"{'Every new punch will be sent as a Telegram message.' if new_val else 'No punch notifications until re-enabled.'}\n"
        f"Use /livepunches again to toggle.",
        parse_mode=ParseMode.HTML
    )


# ── Edit settings helpers ─────────────────────────────────────────────────────

# Per-chat state for edit UI: 'awaiting' is set when we wait for a text reply
_edit_state: dict = {}   # chat_id → {'ctx': 'report'|'daily', 'awaiting': None|'time'|'exc'}

_ALL_FORMATS = ['xlsx', 'png', 'pdf']
_DEFAULT_DAILY_DAYS = '0,1,2,3,6'
# Telegram callback_data has a 64-byte limit.
# Prefix like 'er:dept:' = 8 chars, leaving 56 for dept name.
# We cap at 50 chars to be safe.
_MAX_DEPT_CALLBACK_LEN = 50
CSV_PREVIEW_MAX_CHARS = 3000

SHELL_BASE_WHITELIST = {
    'ls', 'cat', 'uptime', 'df', 'free', 'whoami', 'id', 'pwd',
    'uname', 'date', 'head', 'tail', 'wc', 'echo'
}
SHELL_ROOT_WHITELIST = SHELL_BASE_WHITELIST | {'journalctl'}
SHELL_READ_CMDS = {'ls', 'cat', 'head', 'tail', 'wc'}
SHELL_BLOCKED_PATHS = {'/root', '/proc', '/sys', '/dev', '/run'}
SHELL_BLOCKED_FILE_PATTERNS = {'shadow', '.ssh'}
SHELL_SSH_PRIVATE_KEY_PREFIXES = ('id_rsa', 'id_ed25519', 'id_ecdsa', 'id_dsa')
SQL_SELECT_RE = re.compile(
    r'^\s*select\s+(?P<cols>[a-zA-Z0-9_,\s*]+)\s+from\s+(?P<table>[a-zA-Z_][a-zA-Z0-9_]*)'
    r'(?:\s+where\s+(?P<where>[a-zA-Z0-9_\'"\s=]+))?'
    r'(?:\s+limit\s+(?P<limit>\d+))?\s*$',
    re.IGNORECASE
)
SQL_DISALLOWED_RE = re.compile(
    r'(^|\W)(insert|update|delete|drop|alter|create|truncate|replace|'
    r'grant|revoke|attach|pragma|exec|execute|union)(\W|$)',
    re.IGNORECASE
)
SQL_WHERE_EQ_RE = re.compile(
    r'^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([0-9]+))\s*$'
)


def _is_locked_out(user_id: str) -> tuple:
    st = _shell_lockouts.get(user_id)
    if not st:
        return False, 0
    until = st.get('locked_until')
    if not until or datetime.now() >= until:
        _shell_lockouts.pop(user_id, None)
        return False, 0
    remain = int((until - datetime.now()).total_seconds() // 60) + 1
    return True, remain


def _register_failed_auth(user_id: str):
    st = _shell_lockouts.setdefault(user_id, {'failed': 0, 'locked_until': None})
    st['failed'] = st.get('failed', 0) + 1
    if st['failed'] >= SHELL_MAX_FAILED_ATTEMPTS:
        st['locked_until'] = datetime.now() + timedelta(minutes=SHELL_LOCKOUT_MINUTES)
        st['failed'] = 0


def _clear_failed_auth(user_id: str):
    _shell_lockouts.pop(user_id, None)


def _get_active_shell(chat_id: str):
    sess = _shell_sessions.get(chat_id)
    if not sess:
        return None
    if datetime.now() > sess['expires_at']:
        _shell_sessions.pop(chat_id, None)
        return None
    return sess


def _is_safe_read_path(raw_path: str) -> bool:
    if not raw_path:
        return False
    if raw_path.startswith('-'):
        return False
    if '..' in raw_path:
        return False
    full = os.path.realpath(raw_path if raw_path.startswith('/') else os.path.join(SHELL_BASE_DIR, raw_path))
    for part in SHELL_BLOCKED_PATHS:
        part_full = os.path.realpath(part)
        if full == part_full or full.startswith(part_full + os.sep):
            return False
    if not any(full == ap or full.startswith(ap + os.sep) for ap in SHELL_ALLOWED_PATHS):
        return False
    parts_lower = [p.lower() for p in full.split(os.sep) if p]
    basename = os.path.basename(full).lower()
    if any(mark in parts_lower for mark in SHELL_BLOCKED_FILE_PATTERNS):
        return False
    if basename in {'passwd', 'gshadow'}:
        return False
    if basename.startswith(SHELL_SSH_PRIVATE_KEY_PREFIXES):
        return False
    return True


def _run_safe_shell_command(command_text: str, elevated: bool = False) -> tuple:
    if any(sym in command_text for sym in ['|', '&', ';', '>', '<', '`', '$(', '${', '\n', '\r']):
        return False, 'Command syntax not allowed.'
    try:
        parts = shlex.split(command_text)
    except ValueError:
        return False, 'Unable to parse command.'
    if not parts:
        return False, 'Empty command.'
    cmd = parts[0].lower()
    if any('\\' in arg for arg in parts[1:]):
        return False, 'Backslash escapes are not allowed in arguments.'
    allowed_set = SHELL_ROOT_WHITELIST if elevated else SHELL_BASE_WHITELIST
    if cmd not in allowed_set:
        return False, f'Command "{cmd}" is not allowed.'
    if cmd in SHELL_READ_CMDS:
        args = [a for a in parts[1:] if not a.startswith('-')]
        if not args:
            return False, f'{cmd} requires a file path.'
        for p in args:
            if not _is_safe_read_path(p):
                return False, f'Blocked unsafe path: {p}'
    try:
        proc = subprocess.run(
            parts,
            capture_output=True,
            text=True,
            timeout=SHELL_CMD_TIMEOUT_SECONDS,
            shell=False,
            cwd=SHELL_BASE_DIR,
        )
    except subprocess.TimeoutExpired:
        return False, f'Command timed out after {SHELL_CMD_TIMEOUT_SECONDS}s.'
    except Exception as exc:
        return False, f'Execution error: {str(exc)[:120]}'
    out = (proc.stdout or '').strip()
    err = (proc.stderr or '').strip()
    text = out if out else err
    if not text:
        text = '(no output)'
    if len(text) > 3500:
        text = text[:3500] + '\n...[truncated]'
    if proc.returncode != 0 and not err:
        text = f'Command exited with code {proc.returncode}\n{text}'
    return True, text


def _validate_select_query(query: str) -> tuple:
    q = (query or '').strip()
    if not q:
        return False, 'Query is empty.'
    if len(q) > 500:
        return False, 'Query too long.'
    if ';' in q:
        return False, 'Semicolons are not allowed.'
    if '--' in q or '/*' in q or '*/' in q:
        return False, 'SQL comments are not allowed.'
    if SQL_DISALLOWED_RE.search(q):
        return False, 'Only read-only SELECT statements are allowed.'
    m = SQL_SELECT_RE.match(q)
    if not m:
        return False, 'Only simple SELECT ... FROM ... [WHERE ...] [LIMIT N] syntax is allowed.'
    cols = m.group('cols').strip()
    if '(' in cols or ')' in cols:
        return False, 'Functions/subqueries are not allowed in SELECT columns.'
    return True, ''


def _execute_readonly_sql(query: str) -> tuple:
    ok, err = _validate_select_query(query)
    if not ok:
        return False, err, None
    q = query.strip()
    m = SQL_SELECT_RE.match(q)
    cols_expr = m.group('cols').strip()
    table = m.group('table')
    try:
        tables = {t.lower(): t for t in mdb_reader.list_tables()}
    except Exception as exc:
        return False, f'Failed to inspect MDB tables: {exc}', None
    if table.lower() not in tables:
        return False, f'Unknown table: {table}', None
    table_real = tables[table.lower()]
    try:
        rows = mdb_reader._mdb_export(table_real)
    except Exception as exc:
        return False, f'Failed to read table: {exc}', None
    if not rows:
        return True, 'No rows found.', None
    df = pd.DataFrame(rows)

    where_clause = (m.group('where') or '').strip()
    if where_clause:
        conds = [c.strip() for c in re.split(r'\s+and\s+', where_clause, flags=re.IGNORECASE) if c.strip()]
        for c in conds:
            cm = SQL_WHERE_EQ_RE.match(c)
            if not cm:
                return False, 'WHERE only supports equality with AND (col = value).', None
            col = cm.group(1)
            if col not in df.columns:
                return False, f'Unknown WHERE column: {col}', None
            val = next(v for v in cm.groups()[1:] if v is not None)
            df = df[df[col].astype(str) == str(val)]

    if cols_expr != '*':
        cols = [c.strip() for c in cols_expr.split(',') if c.strip()]
        for c in cols:
            if c not in df.columns:
                return False, f'Unknown SELECT column: {c}', None
        df = df[cols]

    limit = SQL_MAX_ROWS
    if m.group('limit'):
        limit = min(int(m.group('limit')), SQL_MAX_ROWS)
    df = df.head(limit).fillna('')
    text = df.to_string(index=False) if not df.empty else 'No rows found.'
    if len(text) <= SQL_MAX_TEXT_CHARS:
        footer = f'\nRows: {len(df)} (limit {limit}, hard max {SQL_MAX_ROWS})'
        return True, text + footer, None
    csv_bytes = df.to_csv(index=False).encode('utf-8')
    return True, f'Result too large for text. Sending CSV ({len(df)} rows).', BytesIO(csv_bytes)


def _get_dept_list() -> list:
    """Return all known departments, priority first, then rest alphabetically."""
    try:
        dept_map = mdb_reader.get_dept_map()
        all_depts = sorted({v for v in dept_map.values() if v})
    except Exception:
        all_depts = []
    # Sort by priority
    ordered = [d for d in DEPT_ORDER if any(d == x.upper() for x in all_depts)]
    rest    = [d for d in all_depts if d.upper() not in DEPT_ORDER]
    return ordered + rest


def _fmt_report_panel() -> tuple:
    """Return (text, InlineKeyboardMarkup) for the /editreport panel."""
    depts    = settings.get_report_departments()
    fmts     = settings.get_report_formats()
    tpl      = settings.get_report_template()
    tpl_label = REPORT_TEMPLATES.get(tpl, {}).get('label', tpl)
    save_dir  = settings.get_report_save_dir() or '—'

    text = (
        f"📊 <b>On-Demand /report Settings</b>\n\n"
        f"📁 Departments: <b>{depts}</b>\n"
        f"📄 Formats: <b>{fmts.upper()}</b>\n"
        f"🎨 Template: <b>{tpl_label}</b>\n"
        f"💾 Save Dir: <code>{save_dir}</code>"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton('📁 Departments', callback_data='er:dept_menu'),
            InlineKeyboardButton('📄 Format',      callback_data='er:fmt_menu'),
        ],
        [
            InlineKeyboardButton('🎨 Template',    callback_data='er:tmpl_menu'),
            InlineKeyboardButton('💾 Save Dir',    callback_data='er:savedir'),
        ],
        [InlineKeyboardButton('✅ Done', callback_data='er:done')],
    ])
    return text, kb


def _fmt_daily_panel() -> tuple:
    """Return (text, InlineKeyboardMarkup) for the /editdaily panel."""
    depts    = settings.get_daily_departments()
    fmts     = settings.get_daily_formats()
    tpl      = settings.get_daily_template()
    tpl_lbl  = REPORT_TEMPLATES.get(tpl, {}).get('label', tpl)
    exc      = settings.get_daily_exclude_badges() or '—'
    save_dir = settings.get_daily_save_dir() or '—'

    text = (
        f"⏰ <b>Daily Report Settings</b>\n\n"
        f"🕐 Time: <b>{settings.daily_time_label()}</b>\n"
        f"📅 Days: <b>{settings.daily_days_label()}</b>\n"
        f"📁 Departments: <b>{depts}</b>\n"
        f"📄 Formats: <b>{fmts.upper()}</b>\n"
        f"🎨 Template: <b>{tpl_lbl}</b>\n"
        f"🚫 Extra excluded badges: <b>{exc}</b>\n"
        f"💾 Save Dir: <code>{save_dir}</code>"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton('🕐 Time',        callback_data='ed:time'),
            InlineKeyboardButton('📅 Days',        callback_data='ed:days_menu'),
        ],
        [
            InlineKeyboardButton('📁 Departments', callback_data='ed:dept_menu'),
            InlineKeyboardButton('📄 Format',      callback_data='ed:fmt_menu'),
        ],
        [
            InlineKeyboardButton('🎨 Template',    callback_data='ed:tmpl_menu'),
            InlineKeyboardButton('🚫 Exclusions',  callback_data='ed:exc'),
        ],
        [InlineKeyboardButton('💾 Save Dir',       callback_data='ed:savedir')],
        [InlineKeyboardButton('✅ Done',            callback_data='ed:done')],
    ])
    return text, kb


def _dept_menu_kb(ctx_key: str, current: str) -> InlineKeyboardMarkup:
    """Build department selection keyboard for 'er' or 'ed' context."""
    selected = (set() if current.strip().upper() == 'ALL'
                else {d.strip().upper() for d in current.split(',') if d.strip()})
    depts = _get_dept_list()

    rows = []
    # ALL toggle row
    all_checked = not selected
    rows.append([InlineKeyboardButton(
        f"{'✅' if all_checked else '⬜'} ALL",
        callback_data=f'{ctx_key}:dept:ALL',
    )])
    # Individual dept rows (2 per row)
    dept_btns = []
    for dept in depts:
        checked = dept.upper() in selected
        dept_btns.append(InlineKeyboardButton(
            f"{'✅' if checked else '⬜'} {dept}",
            callback_data=f'{ctx_key}:dept:{dept[:_MAX_DEPT_CALLBACK_LEN]}',
        ))
    for i in range(0, len(dept_btns), 2):
        rows.append(dept_btns[i:i + 2])

    rows.append([InlineKeyboardButton('← Back', callback_data=f'{ctx_key}:back')])
    return InlineKeyboardMarkup(rows)


def _fmt_menu_kb(ctx_key: str, current: str) -> InlineKeyboardMarkup:
    selected = {f.strip().lower() for f in current.split(',') if f.strip()}
    rows = []
    fmt_btns = []
    for fmt in _ALL_FORMATS:
        checked = fmt in selected
        fmt_btns.append(InlineKeyboardButton(
            f"{'✅' if checked else '⬜'} {fmt.upper()}",
            callback_data=f'{ctx_key}:fmt:{fmt}',
        ))
    rows.append(fmt_btns)
    # ALL shortcut
    rows.append([InlineKeyboardButton(
        f"{'✅' if selected >= set(_ALL_FORMATS) else '⬜'} ALL",
        callback_data=f'{ctx_key}:fmt:all',
    )])
    rows.append([InlineKeyboardButton('← Back', callback_data=f'{ctx_key}:back')])
    return InlineKeyboardMarkup(rows)


def _tmpl_menu_kb(ctx_key: str, current: str) -> InlineKeyboardMarkup:
    rows = []
    for key, info in REPORT_TEMPLATES.items():
        checked = key == current
        rows.append([InlineKeyboardButton(
            f"{'✅' if checked else '⬜'} {info['label']}",
            callback_data=f'{ctx_key}:tmpl:{key}',
        )])
    rows.append([InlineKeyboardButton('← Back', callback_data=f'{ctx_key}:back')])
    return InlineKeyboardMarkup(rows)


def _days_menu_kb(current: str, ctx_key: str = 'ed') -> InlineKeyboardMarkup:
    selected = {int(d.strip()) for d in current.split(',')
                if d.strip().isdigit()}
    rows = []
    btns = []
    for day_num, day_name in _DAY_NAMES.items():
        checked = day_num in selected
        btns.append(InlineKeyboardButton(
            f"{'✅' if checked else '⬜'} {day_name}",
            callback_data=f'{ctx_key}:day:{day_num}',
        ))
    # 3 per row
    for i in range(0, len(btns), 3):
        rows.append(btns[i:i + 3])
    rows.append([InlineKeyboardButton('← Back', callback_data=f'{ctx_key}:back')])
    return InlineKeyboardMarkup(rows)



# ── Email (SMTP) settings helpers ─────────────────────────────────────────────

_EMAIL_FORMATS = ['html', 'plain', 'both']


def _smtp_status_icon() -> str:
    return '🟢' if settings.get_smtp_enabled() else '🔴'


def _smtp_daily_icon() -> str:
    return '🟢' if settings.get_smtp_daily_enabled() else '🔴'


def _fmt_email_panel() -> tuple:
    """Return (text, InlineKeyboardMarkup) for the /editemail panel."""
    sender      = settings.get_smtp_sender_email() or '—'
    name        = settings.get_smtp_sender_name() or '—'
    password_ok = '✅ Set' if settings.get_smtp_app_password() else '❌ Not set'
    recipients  = settings.get_smtp_recipients()
    recip_str   = f"{len(recipients)} address(es)" if recipients else '—'
    subject     = settings.get_smtp_subject()
    fmt         = settings.get_smtp_format().upper()

    text = (
        f"📧 <b>Email (SMTP) Settings</b>\n\n"
        f"🔌 SMTP: <b>{'ENABLED' if settings.get_smtp_enabled() else 'DISABLED'}</b>\n"
        f"📅 Daily email: <b>{'ENABLED' if settings.get_smtp_daily_enabled() else 'DISABLED'}</b>\n"
        f"🕐 Send time: <b>{settings.daily_time_label()}</b>\n"
        f"📆 Send days: <b>{settings.daily_days_label()}</b>\n\n"
        f"📤 Sender: <code>{sender}</code>\n"
        f"👤 Name: <b>{name}</b>\n"
        f"🔑 Password: <b>{password_ok}</b>\n"
        f"👥 Recipients: <b>{recip_str}</b>\n"
        f"📝 Subject: <code>{subject}</code>\n"
        f"📄 Format: <b>{fmt}</b>"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"{_smtp_status_icon()} SMTP {'On' if settings.get_smtp_enabled() else 'Off'}",
                callback_data='ee:toggle_smtp'),
            InlineKeyboardButton(
                f"{_smtp_daily_icon()} Daily {'On' if settings.get_smtp_daily_enabled() else 'Off'}",
                callback_data='ee:toggle_daily'),
        ],
        [
            InlineKeyboardButton('📤 Sender Email', callback_data='ee:sender_email'),
            InlineKeyboardButton('👤 Sender Name',  callback_data='ee:sender_name'),
        ],
        [
            InlineKeyboardButton('🔑 Password',    callback_data='ee:password'),
            InlineKeyboardButton('👥 Recipients',  callback_data='ee:recipients_menu'),
        ],
        [
            InlineKeyboardButton('📝 Subject',     callback_data='ee:subject'),
            InlineKeyboardButton('📄 Format',      callback_data='ee:fmt_menu'),
        ],
        [
            InlineKeyboardButton('🕐 Send Time',   callback_data='ee:time'),
            InlineKeyboardButton('📆 Send Days',   callback_data='ee:days_menu'),
        ],
        [
            InlineKeyboardButton('📨 Send Now',    callback_data='ee:send_now'),
        ],
        [InlineKeyboardButton('✅ Done',           callback_data='ee:done')],
    ])
    return text, kb


def _email_fmt_menu_kb(current: str) -> InlineKeyboardMarkup:
    rows = []
    for fmt in _EMAIL_FORMATS:
        checked = fmt == current.lower()
        rows.append([InlineKeyboardButton(
            f"{'✅' if checked else '⬜'} {fmt.upper()}",
            callback_data=f'ee:fmt:{fmt}',
        )])
    rows.append([InlineKeyboardButton('← Back', callback_data='ee:back')])
    return InlineKeyboardMarkup(rows)


def _email_recipients_menu_kb() -> InlineKeyboardMarkup:
    recipients = settings.get_smtp_recipients()
    rows = []
    for i, addr in enumerate(recipients):
        rows.append([InlineKeyboardButton(
            f"❌ {addr}",
            callback_data=f'ee:remove_recip:{i}',
        )])
    rows.append([InlineKeyboardButton('➕ Add Recipient', callback_data='ee:add_recipient')])
    rows.append([InlineKeyboardButton('← Back', callback_data='ee:back')])
    return InlineKeyboardMarkup(rows)


def _email_recipients_text() -> str:
    recipients = settings.get_smtp_recipients()
    if not recipients:
        return '👥 <b>Recipients</b>\n\nNo recipients configured yet.'
    addr_list = '\n'.join(f"  {i + 1}. {r}" for i, r in enumerate(recipients))
    return f'👥 <b>Recipients ({len(recipients)})</b>\n\n{addr_list}\n\nTap ❌ to remove, or add a new address:'


# ── /editreport command ───────────────────────────────────────────────────────

async def cmd_editreport(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    chat_id = str(update.effective_chat.id)
    _edit_state[chat_id] = {'ctx': 'report', 'awaiting': None}
    text, kb = _fmt_report_panel()
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# ── /editdaily command ────────────────────────────────────────────────────────

async def cmd_editdaily(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    chat_id = str(update.effective_chat.id)
    _edit_state[chat_id] = {'ctx': 'daily', 'awaiting': None}
    text, kb = _fmt_daily_panel()
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# ── /editemail command ────────────────────────────────────────────────────────

async def cmd_editemail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    chat_id = str(update.effective_chat.id)
    _edit_state[chat_id] = {'ctx': 'email', 'awaiting': None}
    text, kb = _fmt_email_panel()
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# ── /mail command ─────────────────────────────────────────────────────────────

async def _send_email_for_date(query: CallbackQuery, update: Update, report_date: date):
    """Send the absent-report email for *report_date* and confirm in chat."""
    if not settings.get_smtp_enabled():
        await query.edit_message_text(
            '❌ Email delivery is disabled.\n'
            'Enable it first with /editemail.')
        return
    missing = []
    if not settings.get_smtp_sender_email():
        missing.append('sender email')
    if not settings.get_smtp_app_password():
        missing.append('App Password')
    if not settings.get_smtp_recipients():
        missing.append('recipients')
    if missing:
        await query.edit_message_text(
            f'❌ Email not fully configured. Missing: {", ".join(missing)}.\n'
            'Use /editemail to complete setup.')
        return

    await query.edit_message_text(
        f'⏳ Sending email report for {report_date.strftime("%d/%m/%Y")}…')
    try:
        history = mdb_reader.get_history(report_date, report_date)
        absent = history[0]['absent'] if history else []
        summary = {
            'total':         history[0].get('total', 0) if history else 0,
            'present_count': history[0].get('present_count', 0) if history else 0,
            'absent_count':  len(absent),
            'absent':        absent,
        }
        ok, err = email_sender.send_report_email(
            sender_email=settings.get_smtp_sender_email(),
            sender_name=settings.get_smtp_sender_name(),
            app_password=settings.get_smtp_app_password(),
            recipients=settings.get_smtp_recipients(),
            subject=settings.get_smtp_subject(),
            report_date=report_date,
            absent=absent,
            summary=summary,
            fmt=settings.get_smtp_format(),
        )
        n = len(settings.get_smtp_recipients())
        if ok:
            await query.edit_message_text(
                f'✅ Email report for {report_date.strftime("%d/%m/%Y")} '
                f'sent to {n} recipient(s).')
        else:
            await query.edit_message_text(
                f'❌ Email failed: {err}\n\n'
                'Check your SMTP settings with /editemail.')
    except Exception as exc:
        await query.edit_message_text(f'❌ Error: {exc}')


async def cmd_mail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Send an attendance-report email for today or a chosen date."""
    if not _allowed(update):
        return await _deny(update)
    if not settings.get_smtp_enabled():
        await update.message.reply_text(
            '❌ Email delivery is disabled.\n'
            'Enable it first with /editemail.')
        return
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton('📅 Today',     callback_data='mail:today'),
            InlineKeyboardButton('📆 Pick Date', callback_data='mail:pick'),
        ],
        [InlineKeyboardButton('❌ Cancel', callback_data='mail:cancel')],
    ])
    await update.message.reply_text(
        '📧 <b>Email Attendance Report</b>\n\nSelect date:',
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


# ── Edit settings callback handler ────────────────────────────────────────────

async def callback_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    if not _allowed(update):
        return

    data    = query.data or ''
    chat_id = str(update.effective_chat.id)

    # ── On-demand report panel ── (prefix 'er:')
    if data.startswith('er:'):
        action = data[3:]

        if action == 'done':
            _edit_state.pop(chat_id, None)
            await query.edit_message_text('✅ Report settings saved.')
            return

        if action == 'back':
            text, kb = _fmt_report_panel()
            await query.edit_message_text(text, parse_mode=ParseMode.HTML,
                                          reply_markup=kb)
            return

        if action == 'dept_menu':
            kb = _dept_menu_kb('er', settings.get_report_departments())
            await query.edit_message_text(
                '📁 <b>Select Departments</b>\n(tap to toggle)',
                parse_mode=ParseMode.HTML, reply_markup=kb)
            return

        if action == 'fmt_menu':
            kb = _fmt_menu_kb('er', settings.get_report_formats())
            await query.edit_message_text(
                '📄 <b>Select Formats</b>\n(tap to toggle)',
                parse_mode=ParseMode.HTML, reply_markup=kb)
            return

        if action == 'tmpl_menu':
            kb = _tmpl_menu_kb('er', settings.get_report_template())
            await query.edit_message_text(
                '🎨 <b>Select Template</b>',
                parse_mode=ParseMode.HTML, reply_markup=kb)
            return

        if action.startswith('dept:'):
            val = action[5:].strip()
            if val.upper() == 'ALL':
                settings.set_report_departments('ALL')
            else:
                current = settings.get_report_departments()
                sel = (set() if current.strip().upper() == 'ALL'
                       else {d.strip().upper() for d in current.split(',') if d.strip()})
                if val.upper() in sel:
                    sel.discard(val.upper())
                else:
                    sel.add(val.upper())
                settings.set_report_departments(','.join(sorted(sel)) if sel else 'ALL')
            kb = _dept_menu_kb('er', settings.get_report_departments())
            await query.edit_message_reply_markup(reply_markup=kb)
            return

        if action.startswith('fmt:'):
            val = action[4:].strip().lower()
            if val == 'all':
                settings.set_report_formats('xlsx,png,pdf')
            else:
                current = settings.get_report_formats()
                sel = {f.strip().lower() for f in current.split(',') if f.strip()}
                if val in sel:
                    sel.discard(val)
                else:
                    sel.add(val)
                settings.set_report_formats(','.join(sorted(sel)) if sel else 'xlsx')
            kb = _fmt_menu_kb('er', settings.get_report_formats())
            await query.edit_message_reply_markup(reply_markup=kb)
            return

        if action.startswith('tmpl:'):
            val = action[5:].strip()
            settings.set_report_template(val)
            kb = _tmpl_menu_kb('er', settings.get_report_template())
            await query.edit_message_reply_markup(reply_markup=kb)
            return

        if action == 'savedir':
            st = _edit_state.setdefault(chat_id, {})
            st['ctx']      = 'report'
            st['awaiting'] = 'report_savedir'
            current_dir = settings.get_report_save_dir() or '—'
            await query.edit_message_text(
                '💾 <b>Set Save Directory</b>\n\n'
                f'Current: <code>{current_dir}</code>\n\n'
                'Reply with the full path where reports should be saved.\n'
                'Example: <code>C:\\Users\\admin\\Desktop\\Attendance</code>\n'
                'Send <code>none</code> to disable saving.',
                parse_mode=ParseMode.HTML)
            return

    # ── Daily report panel ── (prefix 'ed:')
    if data.startswith('ed:'):
        action = data[3:]

        if action == 'done':
            _edit_state.pop(chat_id, None)
            await query.edit_message_text('✅ Daily report settings saved.')
            return

        if action == 'back':
            text, kb = _fmt_daily_panel()
            await query.edit_message_text(text, parse_mode=ParseMode.HTML,
                                          reply_markup=kb)
            return

        if action == 'time':
            st = _edit_state.setdefault(chat_id, {})
            st['ctx']       = 'daily'
            st['awaiting']  = 'time'
            st['msg_id']    = query.message.message_id
            await query.edit_message_text(
                '🕐 <b>Change Report Time</b>\n\n'
                f'Current: <b>{settings.daily_time_label()}</b>\n\n'
                'Reply with the new time in <b>HH:MM</b> format (24h).\n'
                'Example: <code>08:15</code>',
                parse_mode=ParseMode.HTML)
            return

        if action == 'exc':
            st = _edit_state.setdefault(chat_id, {})
            st['ctx']      = 'daily'
            st['awaiting'] = 'exc'
            st['msg_id']   = query.message.message_id
            current = settings.get_daily_exclude_badges() or '—'
            await query.edit_message_text(
                '🚫 <b>Extra Badge Exclusions</b>\n\n'
                f'Current: <b>{current}</b>\n\n'
                'Reply with comma-separated badge numbers to exclude from the <i>daily report</i>.\n'
                '(These are in addition to the global exclusions in config.ini)\n'
                'Example: <code>1234,5678</code>\n'
                'Send <code>none</code> to clear.',
                parse_mode=ParseMode.HTML)
            return

        if action == 'dept_menu':
            kb = _dept_menu_kb('ed', settings.get_daily_departments())
            await query.edit_message_text(
                '📁 <b>Select Departments</b>\n(tap to toggle)',
                parse_mode=ParseMode.HTML, reply_markup=kb)
            return

        if action == 'fmt_menu':
            kb = _fmt_menu_kb('ed', settings.get_daily_formats())
            await query.edit_message_text(
                '📄 <b>Select Formats</b>\n(tap to toggle)',
                parse_mode=ParseMode.HTML, reply_markup=kb)
            return

        if action == 'tmpl_menu':
            kb = _tmpl_menu_kb('ed', settings.get_daily_template())
            await query.edit_message_text(
                '🎨 <b>Select Template</b>',
                parse_mode=ParseMode.HTML, reply_markup=kb)
            return

        if action == 'days_menu':
            kb = _days_menu_kb(settings.get_daily_days())
            await query.edit_message_text(
                '📅 <b>Select Report Days</b>\n(tap to toggle)',
                parse_mode=ParseMode.HTML, reply_markup=kb)
            return

        if action.startswith('day:'):
            day_num = int(action[4:])
            current = settings.get_daily_days()
            sel = {int(d.strip()) for d in current.split(',') if d.strip().isdigit()}
            if day_num in sel:
                sel.discard(day_num)
            else:
                sel.add(day_num)
            settings.set_daily_days(','.join(str(d) for d in sorted(sel)) if sel else _DEFAULT_DAILY_DAYS)
            kb = _days_menu_kb(settings.get_daily_days())
            await query.edit_message_reply_markup(reply_markup=kb)
            return

        if action.startswith('dept:'):
            val = action[5:].strip()
            if val.upper() == 'ALL':
                settings.set_daily_departments('ALL')
            else:
                current = settings.get_daily_departments()
                sel = (set() if current.strip().upper() == 'ALL'
                       else {d.strip().upper() for d in current.split(',') if d.strip()})
                if val.upper() in sel:
                    sel.discard(val.upper())
                else:
                    sel.add(val.upper())
                settings.set_daily_departments(','.join(sorted(sel)) if sel else 'ALL')
            kb = _dept_menu_kb('ed', settings.get_daily_departments())
            await query.edit_message_reply_markup(reply_markup=kb)
            return

        if action.startswith('fmt:'):
            val = action[4:].strip().lower()
            if val == 'all':
                settings.set_daily_formats('xlsx,png,pdf')
            else:
                current = settings.get_daily_formats()
                sel = {f.strip().lower() for f in current.split(',') if f.strip()}
                if val in sel:
                    sel.discard(val)
                else:
                    sel.add(val)
                settings.set_daily_formats(','.join(sorted(sel)) if sel else 'xlsx')
            kb = _fmt_menu_kb('ed', settings.get_daily_formats())
            await query.edit_message_reply_markup(reply_markup=kb)
            return

        if action.startswith('tmpl:'):
            val = action[5:].strip()
            settings.set_daily_template(val)
            kb = _tmpl_menu_kb('ed', settings.get_daily_template())
            await query.edit_message_reply_markup(reply_markup=kb)
            return

        if action == 'savedir':
            st = _edit_state.setdefault(chat_id, {})
            st['ctx']      = 'daily'
            st['awaiting'] = 'daily_savedir'
            current_dir = settings.get_daily_save_dir() or '—'
            await query.edit_message_text(
                '💾 <b>Set Daily Report Save Directory</b>\n\n'
                f'Current: <code>{current_dir}</code>\n\n'
                'Reply with the full path where daily reports should be saved.\n'
                'Example: <code>C:\\Users\\admin\\Desktop\\Attendance\\Auto Daily Attendance</code>\n'
                'Send <code>none</code> to disable saving.',
                parse_mode=ParseMode.HTML)
            return

    # ── Email settings panel ── (prefix 'ee:')
    if data.startswith('ee:'):
        action = data[3:]

        if action == 'done':
            _edit_state.pop(chat_id, None)
            await query.edit_message_text('✅ Email settings saved.')
            return

        if action == 'back':
            text, kb = _fmt_email_panel()
            await query.edit_message_text(text, parse_mode=ParseMode.HTML,
                                          reply_markup=kb)
            return

        if action == 'toggle_smtp':
            settings.set_smtp_enabled(not settings.get_smtp_enabled())
            text, kb = _fmt_email_panel()
            await query.edit_message_text(text, parse_mode=ParseMode.HTML,
                                          reply_markup=kb)
            return

        if action == 'toggle_daily':
            settings.set_smtp_daily_enabled(not settings.get_smtp_daily_enabled())
            text, kb = _fmt_email_panel()
            await query.edit_message_text(text, parse_mode=ParseMode.HTML,
                                          reply_markup=kb)
            return

        if action == 'sender_email':
            st = _edit_state.setdefault(chat_id, {})
            st['ctx']      = 'email'
            st['awaiting'] = 'email_sender_email'
            current = settings.get_smtp_sender_email() or '—'
            await query.edit_message_text(
                '📤 <b>Set Sender Email</b>\n\n'
                f'Current: <code>{current}</code>\n\n'
                'Reply with your Gmail address.\n'
                'Example: <code>school.attendance@gmail.com</code>',
                parse_mode=ParseMode.HTML)
            return

        if action == 'sender_name':
            st = _edit_state.setdefault(chat_id, {})
            st['ctx']      = 'email'
            st['awaiting'] = 'email_sender_name'
            current = settings.get_smtp_sender_name()
            await query.edit_message_text(
                '👤 <b>Set Sender Name</b>\n\n'
                f'Current: <b>{current}</b>\n\n'
                'Reply with the display name for the From: header.\n'
                'Example: <code>Attendance Bot</code>',
                parse_mode=ParseMode.HTML)
            return

        if action == 'password':
            st = _edit_state.setdefault(chat_id, {})
            st['ctx']      = 'email'
            st['awaiting'] = 'email_app_password'
            set_status = '✅ Already set' if settings.get_smtp_app_password() else '❌ Not set'
            await query.edit_message_text(
                '🔑 <b>Set Gmail App Password</b>\n\n'
                f'Status: <b>{set_status}</b>\n\n'
                'Reply with your Gmail App Password (16 chars, no spaces).\n'
                'Get one at: Google Account → Security → App Passwords.\n\n'
                '⚠️ This is stored in config.ini — do not share that file.',
                parse_mode=ParseMode.HTML)
            return

        if action == 'recipients_menu':
            kb = _email_recipients_menu_kb()
            await query.edit_message_text(
                _email_recipients_text(),
                parse_mode=ParseMode.HTML, reply_markup=kb)
            return

        if action == 'add_recipient':
            st = _edit_state.setdefault(chat_id, {})
            st['ctx']      = 'email'
            st['awaiting'] = 'email_add_recipient'
            await query.edit_message_text(
                '➕ <b>Add Recipient</b>\n\n'
                'Reply with the email address to add.\n'
                'Example: <code>principal@school.ae</code>',
                parse_mode=ParseMode.HTML)
            return

        if action.startswith('remove_recip:'):
            try:
                idx = int(action.split(':')[1])
            except (ValueError, IndexError):
                return
            recipients = settings.get_smtp_recipients()
            if 0 <= idx < len(recipients):
                removed = recipients.pop(idx)
                settings.set_smtp_recipients(recipients)
                kb = _email_recipients_menu_kb()
                await query.edit_message_text(
                    _email_recipients_text() + f'\n\n✅ Removed: <code>{removed}</code>',
                    parse_mode=ParseMode.HTML, reply_markup=kb)
            return

        if action == 'subject':
            st = _edit_state.setdefault(chat_id, {})
            st['ctx']      = 'email'
            st['awaiting'] = 'email_subject'
            current = settings.get_smtp_subject()
            await query.edit_message_text(
                '📝 <b>Set Email Subject</b>\n\n'
                f'Current: <code>{current}</code>\n\n'
                'Reply with the subject line.\n'
                'Use <code>{date}</code> as a placeholder for the report date.\n'
                'Example: <code>Daily Absent Report - {date}</code>',
                parse_mode=ParseMode.HTML)
            return

        if action == 'fmt_menu':
            kb = _email_fmt_menu_kb(settings.get_smtp_format())
            await query.edit_message_text(
                '📄 <b>Select Email Format</b>\n\n'
                '<b>HTML</b> — rich email with a styled table\n'
                '<b>PLAIN</b> — plain-text only\n'
                '<b>BOTH</b> — multipart (HTML + plain fallback)',
                parse_mode=ParseMode.HTML, reply_markup=kb)
            return

        if action == 'time':
            st = _edit_state.setdefault(chat_id, {})
            st['ctx']      = 'email'
            st['awaiting'] = 'email_time'
            current = settings.daily_time_label()
            await query.edit_message_text(
                '🕐 <b>Set Email Send Time</b>\n\n'
                f'Current: <b>{current}</b>\n\n'
                'Reply with the new time in <b>HH:MM</b> format (24h, zero-padded).\n'
                'Example: <code>08:15</code>',
                parse_mode=ParseMode.HTML)
            return

        if action == 'days_menu':
            kb = _days_menu_kb(settings.get_daily_days(), 'ee')
            await query.edit_message_text(
                '📆 <b>Select Email Send Days</b>\n(tap to toggle)',
                parse_mode=ParseMode.HTML, reply_markup=kb)
            return

        if action.startswith('day:'):
            day_num = int(action[4:])
            current = settings.get_daily_days()
            sel = {int(d.strip()) for d in current.split(',') if d.strip().isdigit()}
            if day_num in sel:
                sel.discard(day_num)
            else:
                sel.add(day_num)
            settings.set_daily_days(','.join(str(d) for d in sorted(sel)) if sel else _DEFAULT_DAILY_DAYS)
            kb = _days_menu_kb(settings.get_daily_days(), 'ee')
            await query.edit_message_reply_markup(reply_markup=kb)
            await query.message.reply_text(
                f'✅ Email send days updated to: <b>{settings.daily_days_label()}</b>',
                parse_mode=ParseMode.HTML)
            return

        if action.startswith('fmt:'):
            val = action[4:].strip().lower()
            if val in _EMAIL_FORMATS:
                settings.set_smtp_format(val)
            kb = _email_fmt_menu_kb(settings.get_smtp_format())
            await query.edit_message_reply_markup(reply_markup=kb)
            return

        if action == 'send_now':
            await query.edit_message_text('⏳ Sending email report now…')
            try:
                summary = mdb_reader.get_today_summary()
                absent  = summary['absent']
                ok, err = email_sender.send_report_email(
                    sender_email=settings.get_smtp_sender_email(),
                    sender_name=settings.get_smtp_sender_name(),
                    app_password=settings.get_smtp_app_password(),
                    recipients=settings.get_smtp_recipients(),
                    subject=settings.get_smtp_subject(),
                    report_date=date.today(),
                    absent=absent,
                    summary=summary,
                    fmt=settings.get_smtp_format(),
                )
                if ok:
                    n = len(settings.get_smtp_recipients())
                    await query.edit_message_text(
                        f'✅ Email sent to {n} recipient(s).\n\n'
                        f'Use /editemail to return to settings.')
                else:
                    await query.edit_message_text(
                        f'❌ Email failed: {err}\n\n'
                        'Check your SMTP settings with /editemail.')
            except Exception as exc:
                await query.edit_message_text(f'❌ Error: {exc}')
            return


# ── Text input handler for awaiting states ────────────────────────────────────

async def handle_text_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Capture plain text input when an edit panel is awaiting a value."""
    if not _allowed(update):
        return
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    text = (update.message.text or '').strip()

    # ── Shell password prompts (/shell and /su) ───────────────────────────────
    auth = _shell_auth_state.get(chat_id)
    if auth:
        if datetime.now() > auth.get('expires_at', datetime.now()):
            _shell_auth_state.pop(chat_id, None)
            await update.message.reply_text('⌛ Shell authentication expired. Send /shell again.')
            return
        if auth.get('user_id') != user_id:
            return
        stage = auth.get('stage')
        if stage == 'shell':
            if secrets.compare_digest(text, SHELL_PASSWORD):
                _shell_auth_state.pop(chat_id, None)
                _clear_failed_auth(user_id)
                _shell_sessions[chat_id] = {
                    'user_id': user_id,
                    'started_at': datetime.now(),
                    'expires_at': datetime.now() + timedelta(minutes=SHELL_SESSION_TIMEOUT_MINUTES),
                    'elevated': False,
                }
                _audit('shell.auth.success', update, 'shell unlocked')
                await update.message.reply_text(
                    f'✅ Shell unlocked for {SHELL_SESSION_TIMEOUT_MINUTES} minutes.\n'
                    'Allowed commands only. Send /exit to end. Send su or /su to elevate.'
                )
            else:
                _register_failed_auth(user_id)
                locked, mins = _is_locked_out(user_id)
                _audit('shell.auth.failed', update, f'locked={locked}')
                _shell_auth_state.pop(chat_id, None)
                if locked:
                    await update.message.reply_text(f'⛔ Too many attempts. Locked for {mins} minute(s).')
                else:
                    await update.message.reply_text('❌ Incorrect shell password.')
            return
        if stage == 'su':
            sess = _get_active_shell(chat_id)
            if not sess or sess.get('user_id') != user_id:
                _shell_auth_state.pop(chat_id, None)
                await update.message.reply_text('❌ No active shell session. Start with /shell.')
                return
            if secrets.compare_digest(text, SHELL_ROOT_PASSWORD):
                _shell_auth_state.pop(chat_id, None)
                _clear_failed_auth(user_id)
                sess['elevated'] = True
                sess['expires_at'] = datetime.now() + timedelta(minutes=SHELL_SESSION_TIMEOUT_MINUTES)
                _audit('shell.su.success', update, 'session elevated')
                await update.message.reply_text('✅ Session elevated. Root-level safe whitelist is active.')
            else:
                _register_failed_auth(user_id)
                locked, mins = _is_locked_out(user_id)
                _audit('shell.su.failed', update, f'locked={locked}')
                _shell_auth_state.pop(chat_id, None)
                if locked:
                    await update.message.reply_text(f'⛔ Too many attempts. Locked for {mins} minute(s).')
                else:
                    await update.message.reply_text('❌ Incorrect root password.')
            return

    # ── Shell command mode ────────────────────────────────────────────────────
    sess = _get_active_shell(chat_id)
    if sess:
        if sess.get('user_id') != user_id:
            await update.message.reply_text('⛔ Another admin is currently using /shell in this chat.')
            return
        if text.lower() in ('/exit', 'exit'):
            _shell_sessions.pop(chat_id, None)
            _audit('shell.end', update, 'session ended by text exit')
            await update.message.reply_text('✅ Shell session ended.')
            return
        if text.lower() in ('su', '/su'):
            if not SHELL_ROOT_PASSWORD:
                await update.message.reply_text('❌ Root escalation is not configured.')
                return
            _shell_auth_state[chat_id] = {
                'user_id': user_id,
                'stage': 'su',
                'expires_at': datetime.now() + timedelta(minutes=2),
            }
            _audit('shell.su.prompt', update, 'requested via text su')
            await update.message.reply_text('🔒 Enter root shell password to elevate this session.')
            return
        ok, output = _run_safe_shell_command(text, elevated=bool(sess.get('elevated')))
        sess['expires_at'] = datetime.now() + timedelta(minutes=SHELL_SESSION_TIMEOUT_MINUTES)
        _audit('shell.cmd', update, f'ok={ok} elevated={bool(sess.get("elevated", False))} cmd={text[:120]}')
        status = '✅' if ok else '❌'
        await update.message.reply_text(
            f'{status} <code>{html.escape(output)}</code>',
            parse_mode=ParseMode.HTML
        )
        return

    # ── SQL prompt mode ───────────────────────────────────────────────────────
    sql_prompt = _sql_prompt_state.get(chat_id)
    if sql_prompt:
        if datetime.now() > sql_prompt.get('expires_at', datetime.now()):
            _sql_prompt_state.pop(chat_id, None)
            await update.message.reply_text('⌛ SQL prompt expired. Send /sql again.')
            return
        if sql_prompt.get('user_id') != user_id:
            return
        if text.lower() in ('/exit', 'exit'):
            _sql_prompt_state.pop(chat_id, None)
            _audit('sql.prompt.end', update, 'sql prompt ended by text exit')
            await update.message.reply_text('✅ SQL prompt ended.')
            return
        ok, result, csv_buf = _execute_readonly_sql(text)
        _audit('sql.query', update, f'ok={ok} query={text[:180]}')
        if not ok:
            await update.message.reply_text(f'❌ {result}')
            return
        await update.message.reply_text(f'✅ <b>SQL Result</b>\n<code>{html.escape(result)}</code>', parse_mode=ParseMode.HTML)
        if csv_buf:
            csv_buf.seek(0)
            await update.message.reply_document(
                document=csv_buf,
                filename=f"sql_result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                caption='Read-only SQL result (CSV)'
            )
        return

    device_prompt = _device_state.get(chat_id)
    if device_prompt:
        if device_prompt.get('user_id') != user_id:
            return
        action = device_prompt.get('action')
        step = device_prompt.get('step')
        devices = settings.get_devices()

        if action == 'add':
            draft = device_prompt.setdefault('draft', {})
            if step == 'ip':
                try:
                    ip = _validate_device_ip(text, devices)
                except ValueError as exc:
                    await update.message.reply_text(f'❌ {exc}')
                    return
                draft['ip'] = ip
                device_prompt['step'] = 'name'
                await update.message.reply_text(
                    '✅ IP accepted.\nNow send the device display name.',
                    parse_mode=ParseMode.HTML,
                )
                return
            if step == 'name':
                try:
                    name = _validate_device_name(text, devices)
                except ValueError as exc:
                    await update.message.reply_text(f'❌ {exc}')
                    return
                draft['name'] = name
                device_prompt['step'] = 'port'
                await update.message.reply_text(
                    '✅ Name accepted.\nNow send the device port (example: <code>4370</code>).',
                    parse_mode=ParseMode.HTML,
                )
                return
            if step == 'port':
                try:
                    port = _validate_device_port(text)
                except ValueError as exc:
                    await update.message.reply_text(f'❌ {exc}')
                    return
                ok, err = zk_devices.check_device_connectivity(draft['ip'], port)
                if not ok:
                    await update.message.reply_text(
                        f'❌ Could not reach <code>{html.escape(draft["ip"])}:{port}</code>.\n'
                        f'<i>{html.escape(err[:160] or "Connection failed")}</i>\n\n'
                        'Send a different port, or /device to cancel and restart.',
                        parse_mode=ParseMode.HTML,
                    )
                    return
                draft['port'] = port
                devices.append({
                    'ip': draft['ip'],
                    'name': draft['name'],
                    'port': port,
                })
                settings.save_devices(devices)
                _device_state.pop(chat_id, None)
                _audit('device.add', update,
                       f'name={draft["name"]} ip={draft["ip"]} port={port}')
                await _send_device_panel(
                    update.effective_chat,
                    prefix=(
                        f'✅ Added <b>{html.escape(draft["name"])}</b> '
                        f'(<code>{html.escape(draft["ip"])}:{port}</code>) and verified connectivity.'
                    ),
                )
                return

        if action in ('edit', 'rename'):
            idx = device_prompt.get('device_index', -1)
            if idx < 0 or idx >= len(devices):
                _device_state.pop(chat_id, None)
                await update.message.reply_text('❌ Device no longer exists. Send /device to refresh.')
                return
            current = devices[idx]

            if action == 'rename' or step == 'name':
                try:
                    new_name = _validate_device_name(text, devices, exclude_index=idx)
                except ValueError as exc:
                    await update.message.reply_text(f'❌ {exc}')
                    return
                old_name = current['name']
                current['name'] = new_name
                settings.save_devices(devices)
                _device_state.pop(chat_id, None)
                event = 'device.rename' if action == 'rename' else 'device.edit'
                _audit(event, update,
                       f'field=name old={old_name} new={new_name} ip={current["ip"]} port={current["port"]}')
                await _send_device_panel(
                    update.effective_chat,
                    prefix=f'✅ Renamed <b>{html.escape(old_name)}</b> to <b>{html.escape(new_name)}</b>.',
                )
                return

            if step == 'ip':
                try:
                    new_ip = _validate_device_ip(text, devices, exclude_index=idx)
                except ValueError as exc:
                    await update.message.reply_text(f'❌ {exc}')
                    return
                ok, err = zk_devices.check_device_connectivity(new_ip, current['port'])
                if not ok:
                    await update.message.reply_text(
                        f'❌ Could not reach <code>{html.escape(new_ip)}:{current["port"]}</code>.\n'
                        f'<i>{html.escape(err[:160] or "Connection failed")}</i>\n\n'
                        'Send another IP address.',
                        parse_mode=ParseMode.HTML,
                    )
                    return
                old_ip = current['ip']
                current['ip'] = new_ip
                settings.save_devices(devices)
                _device_state.pop(chat_id, None)
                _audit('device.edit', update,
                       f'field=ip name={current["name"]} old={old_ip} new={new_ip} port={current["port"]}')
                await _send_device_panel(
                    update.effective_chat,
                    prefix=(
                        f'✅ Updated IP for <b>{html.escape(current["name"])}</b> '
                        f'from <code>{html.escape(old_ip)}</code> to <code>{html.escape(new_ip)}</code>.'
                    ),
                )
                return

            if step == 'port':
                try:
                    new_port = _validate_device_port(text)
                except ValueError as exc:
                    await update.message.reply_text(f'❌ {exc}')
                    return
                ok, err = zk_devices.check_device_connectivity(current['ip'], new_port)
                if not ok:
                    await update.message.reply_text(
                        f'❌ Could not reach <code>{html.escape(current["ip"])}:{new_port}</code>.\n'
                        f'<i>{html.escape(err[:160] or "Connection failed")}</i>\n\n'
                        'Send another port.',
                        parse_mode=ParseMode.HTML,
                    )
                    return
                old_port = current['port']
                current['port'] = new_port
                settings.save_devices(devices)
                _device_state.pop(chat_id, None)
                _audit('device.edit', update,
                       f'field=port name={current["name"]} ip={current["ip"]} old={old_port} new={new_port}')
                await _send_device_panel(
                    update.effective_chat,
                    prefix=(
                        f'✅ Updated port for <b>{html.escape(current["name"])}</b> '
                        f'from <code>{old_port}</code> to <code>{new_port}</code>.'
                    ),
                )
                return

    # ── Backup prompt states ──────────────────────────────────────────────────
    bk = _bk_state.get(chat_id)
    if bk and bk.get('awaiting'):
        awaiting_bk = bk['awaiting']

        if awaiting_bk in ('bk_copy_dir', 'bk_copy_dir_now'):
            if text.lower() in ('none', 'clear', '—'):
                settings.set_backup_copy_dir('')
                bk['awaiting'] = None
                _audit('dbbackup.copydir.clear', update)
                await update.message.reply_text(
                    '✅ Backup copy directory cleared.',
                    parse_mode=ParseMode.HTML)
            else:
                settings.set_backup_copy_dir(text)
                _audit('dbbackup.copydir.set', update, f'dir={text}')
                if awaiting_bk == 'bk_copy_dir_now':
                    # Perform the copy immediately after setting dir
                    local_path, _, accessible = _get_mdb_size_info()
                    if not accessible:
                        bk['awaiting'] = None
                        await update.message.reply_text('❌ MDB is not accessible. Check /mdbinfo.')
                        return
                    stamp    = datetime.now().strftime('%Y%m%d_%H%M%S')
                    filename = f"mdb_backup_{stamp}.mdb"
                    try:
                        os.makedirs(text, exist_ok=True)
                        dest = os.path.join(text, filename)
                        shutil.copy2(local_path, dest)
                        _audit('dbbackup.copy', update, f'dest={dest}')
                        await update.message.reply_text(
                            f'✅ Directory saved and MDB copied to:\n<code>{dest}</code>',
                            parse_mode=ParseMode.HTML)
                    except OSError as e:
                        _audit('dbbackup.copy.error', update, str(e))
                        await update.message.reply_text(f'❌ Copy failed: {e}')
                else:
                    await update.message.reply_text(
                        f'✅ Backup copy directory set to:\n<code>{text}</code>',
                        parse_mode=ParseMode.HTML)
                bk['awaiting'] = None
            return

        if awaiting_bk == 'bk_time':
            try:
                parts = text.split(':')
                if len(parts) != 2:
                    raise ValueError('wrong format')
                hh, mm = parts[0].strip(), parts[1].strip()
                if not hh.isdigit() or not mm.isdigit():
                    raise ValueError('non-numeric')
                h, m = int(hh), int(mm)
                if not (0 <= h <= 23 and 0 <= m <= 59):
                    raise ValueError('out of range')
                settings.set_backup_hour(h)
                settings.set_backup_minute(m)
                bk['awaiting'] = None
                _audit('dbbackup.sched.time', update, f'time={h:02d}:{m:02d}')
                await update.message.reply_text(
                    f'✅ Backup schedule time set to <b>{h:02d}:{m:02d}</b>\n'
                    'Use /dbbackup → ⚙️ Schedule Settings to review.',
                    parse_mode=ParseMode.HTML)
            except ValueError:
                await update.message.reply_text(
                    '❌ Invalid format. Please send time as <code>HH:MM</code> '
                    '(24 h, zero-padded), e.g. <code>07:00</code>.',
                    parse_mode=ParseMode.HTML)
            return

        if awaiting_bk == 'bk_sender':
            if text.lower() in ('none', 'clear', '—'):
                settings.set_backup_sender_email('')
                bk['awaiting'] = None
                _audit('dbbackup.mail.sender.clear', update)
                await update.message.reply_text(
                    '✅ Backup sender email cleared — smtp fallback will be used.',
                    parse_mode=ParseMode.HTML)
            elif '@' not in text or '.' not in text.split('@')[-1]:
                await update.message.reply_text(
                    '❌ That does not look like a valid email address. Please try again.',
                    parse_mode=ParseMode.HTML)
                return
            else:
                settings.set_backup_sender_email(text)
                bk['awaiting'] = None
                _audit('dbbackup.mail.sender', update, f'email={text}')
                await update.message.reply_text(
                    f'✅ Backup sender email set to <code>{text}</code>',
                    parse_mode=ParseMode.HTML)
            return

        if awaiting_bk == 'bk_name':
            if text.lower() in ('none', 'clear', '—'):
                settings.set_backup_sender_name('')
                bk['awaiting'] = None
                _audit('dbbackup.mail.name.clear', update)
                await update.message.reply_text(
                    '✅ Backup sender name cleared — smtp fallback will be used.',
                    parse_mode=ParseMode.HTML)
            else:
                settings.set_backup_sender_name(text)
                bk['awaiting'] = None
                _audit('dbbackup.mail.name', update, f'name={text}')
                await update.message.reply_text(
                    f'✅ Backup sender name set to <b>{text}</b>',
                    parse_mode=ParseMode.HTML)
            return

        if awaiting_bk == 'bk_pass':
            if text.lower() in ('none', 'clear', '—'):
                settings.set_backup_app_password('')
                bk['awaiting'] = None
                _audit('dbbackup.mail.pass.clear', update)
                await update.message.reply_text(
                    '✅ Backup app password cleared — smtp fallback will be used.',
                    parse_mode=ParseMode.HTML)
            elif len(text) < 8:
                await update.message.reply_text(
                    '❌ App Password seems too short. Gmail App Passwords are 16 characters. '
                    'Please try again.', parse_mode=ParseMode.HTML)
                return
            else:
                settings.set_backup_app_password(text)
                bk['awaiting'] = None
                _audit('dbbackup.mail.pass.set', update)
                await update.message.reply_text(
                    '✅ Backup app password saved.',
                    parse_mode=ParseMode.HTML)
            return

        if awaiting_bk == 'bk_add_recip':
            if '@' not in text or '.' not in text.split('@')[-1]:
                await update.message.reply_text(
                    '❌ That does not look like a valid email address. Please try again.',
                    parse_mode=ParseMode.HTML)
                return
            recipients = settings.get_backup_recipients_raw()
            if text in recipients:
                bk['awaiting'] = None
                await update.message.reply_text(
                    f'ℹ️ <code>{text}</code> is already in the backup recipients list.',
                    parse_mode=ParseMode.HTML)
                return
            recipients.append(text)
            settings.set_backup_recipients(recipients)
            bk['awaiting'] = None
            _audit('dbbackup.mail.recip.add', update, f'email={text}')
            await update.message.reply_text(
                f'✅ Added <code>{text}</code> to backup recipients.\n'
                f'Total: {len(recipients)}',
                parse_mode=ParseMode.HTML)
            return

    st = _edit_state.get(chat_id)
    if not st or not st.get('awaiting'):
        return  # nothing awaiting — ignore

    awaiting = st['awaiting']

    if awaiting == 'time':
        # Expect HH:MM
        try:
            parts = text.split(':')
            if len(parts) != 2:
                raise ValueError('wrong format')
            h, m = int(parts[0]), int(parts[1])
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError('out of range')
            settings.set_daily_hour(h)
            settings.set_daily_minute(m)
            st['awaiting'] = None
            await update.message.reply_text(
                f"✅ Daily report time set to <b>{h:02d}:{m:02d}</b>\n"
                f"Use /editdaily to review all settings.",
                parse_mode=ParseMode.HTML)
        except Exception:
            await update.message.reply_text(
                '❌ Invalid format. Please send time as <code>HH:MM</code> (e.g. <code>08:15</code>).',
                parse_mode=ParseMode.HTML)
        return

    if awaiting == 'exc':
        if text.lower() in ('none', 'clear', '—'):
            settings.set_daily_exclude_badges('')
            st['awaiting'] = None
            await update.message.reply_text('✅ Extra badge exclusions cleared.')
        else:
            raw_badges = [b.strip() for b in text.replace(' ', ',').split(',')
                          if b.strip()]
            invalid = [b for b in raw_badges if not b.isdigit()]
            if invalid:
                await update.message.reply_text(
                    f"❌ Badge numbers must be numeric. Invalid: {', '.join(invalid)}\n"
                    "Please try again.",
                    parse_mode=ParseMode.HTML)
                return
            badges = ','.join(raw_badges)
            settings.set_daily_exclude_badges(badges)
            st['awaiting'] = None
            await update.message.reply_text(
                f"✅ Extra excluded badges set to: <b>{badges}</b>",
                parse_mode=ParseMode.HTML)
        return

    if awaiting == 'report_savedir':
        if text.lower() in ('none', 'clear', '—'):
            settings.set_report_save_dir('')
            st['awaiting'] = None
            await update.message.reply_text(
                '✅ Report save directory cleared. Files will not be saved locally.',
                parse_mode=ParseMode.HTML)
        else:
            settings.set_report_save_dir(text)
            st['awaiting'] = None
            await update.message.reply_text(
                f'✅ Report save directory set to:\n<code>{text}</code>\n\n'
                f'Use /editreport to review all settings.',
                parse_mode=ParseMode.HTML)
        return

    if awaiting == 'daily_savedir':
        if text.lower() in ('none', 'clear', '—'):
            settings.set_daily_save_dir('')
            st['awaiting'] = None
            await update.message.reply_text(
                '✅ Daily report save directory cleared. Files will not be saved locally.',
                parse_mode=ParseMode.HTML)
        else:
            settings.set_daily_save_dir(text)
            st['awaiting'] = None
            await update.message.reply_text(
                f'✅ Daily report save directory set to:\n<code>{text}</code>\n\n'
                f'Use /editdaily to review all settings.',
                parse_mode=ParseMode.HTML)
        return

    # ── Email (SMTP) awaiting states ──────────────────────────────────────────

    if awaiting == 'email_sender_email':
        if '@' not in text or '.' not in text.split('@')[-1]:
            await update.message.reply_text(
                '❌ That does not look like a valid email address. Please try again.',
                parse_mode=ParseMode.HTML)
            return
        settings.set_smtp_sender_email(text)
        st['awaiting'] = None
        await update.message.reply_text(
            f'✅ Sender email set to <code>{text}</code>\n'
            'Use /editemail to review all settings.',
            parse_mode=ParseMode.HTML)
        return

    if awaiting == 'email_time':
        try:
            parts = text.split(':')
            if len(parts) != 2:
                raise ValueError('wrong format')
            hh, mm = parts[0].strip(), parts[1].strip()
            if len(hh) != 2 or len(mm) != 2 or not hh.isdigit() or not mm.isdigit():
                raise ValueError('wrong format')
            h, m = int(hh), int(mm)
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError('out of range')
            settings.set_daily_hour(h)
            settings.set_daily_minute(m)
            st['awaiting'] = None
            await update.message.reply_text(
                f'✅ Email send time set to <b>{h:02d}:{m:02d}</b>\n'
                'Use /editemail to review all settings.',
                parse_mode=ParseMode.HTML)
        except ValueError:
            await update.message.reply_text(
                '❌ Invalid format. Please send time as <code>HH:MM</code> (24h, zero-padded), '
                'e.g. <code>08:15</code>.',
                parse_mode=ParseMode.HTML)
        return

    if awaiting == 'email_sender_name':
        settings.set_smtp_sender_name(text)
        st['awaiting'] = None
        await update.message.reply_text(
            f'✅ Sender name set to <b>{text}</b>\n'
            'Use /editemail to review all settings.',
            parse_mode=ParseMode.HTML)
        return

    if awaiting == 'email_app_password':
        if len(text) < 8:
            await update.message.reply_text(
                '❌ App Password seems too short. Gmail App Passwords are 16 characters. '
                'Please try again.',
                parse_mode=ParseMode.HTML)
            return
        settings.set_smtp_app_password(text)
        st['awaiting'] = None
        await update.message.reply_text(
            '✅ App Password saved.\n'
            'Use /editemail to review all settings.',
            parse_mode=ParseMode.HTML)
        return

    if awaiting == 'email_add_recipient':
        if '@' not in text or '.' not in text.split('@')[-1]:
            await update.message.reply_text(
                '❌ That does not look like a valid email address. Please try again.',
                parse_mode=ParseMode.HTML)
            return
        recipients = settings.get_smtp_recipients()
        if text in recipients:
            st['awaiting'] = None
            await update.message.reply_text(
                f'ℹ️ <code>{text}</code> is already in the recipients list.',
                parse_mode=ParseMode.HTML)
            return
        recipients.append(text)
        settings.set_smtp_recipients(recipients)
        st['awaiting'] = None
        await update.message.reply_text(
            f'✅ Added <code>{text}</code> to recipients.\n'
            f'Total recipients: {len(recipients)}\n'
            'Use /editemail to review all settings.',
            parse_mode=ParseMode.HTML)
        return

    if awaiting == 'email_subject':
        settings.set_smtp_subject(text)
        st['awaiting'] = None
        await update.message.reply_text(
            f'✅ Subject set to:\n<code>{text}</code>\n\n'
            'Use /editemail to review all settings.',
            parse_mode=ParseMode.HTML)
        return

    if awaiting == 'admin_notice':
        st['awaiting'] = None
        _audit('admin.notice', update, f'len={len(text)}')
        await ctx.bot.send_message(
            chat_id=int(CHAT_ID),
            text=f'📢 <b>Admin Notice</b>\n\n{html.escape(text)}',
            parse_mode=ParseMode.HTML
        )
        await update.message.reply_text('✅ Notice sent.')
        return

# ── Employee commands ─────────────────────────────────────────────────────────

async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage: /search <name or badge>"""
    if not _allowed(update):
        return await _deny(update)
    if not ctx.args:
        await update.message.reply_text('Usage: /search &lt;name or badge&gt;',
                                        parse_mode=ParseMode.HTML)
        return
    query = ' '.join(ctx.args)
    try:
        results = mdb_reader.search_employee(query)
        if not results:
            await update.message.reply_text(f'No employees found for "{query}"')
            return
        lines = [f"🔍 <b>Search: {query}</b>\n"]
        for e in results[:20]:
            status = '✅' if e['active'] else '⛔'
            lines.append(f"{status} [{e['badge']}] {e['name']} — {e['dept']}")
        if len(results) > 20:
            lines.append(f"\n<i>...and {len(results)-20} more</i>")
        await update.message.reply_text('\n'.join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def cmd_punches(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage: /punches <badge>"""
    if not _allowed(update):
        return await _deny(update)
    if not ctx.args:
        await update.message.reply_text('Usage: /punches &lt;badge&gt;',
                                        parse_mode=ParseMode.HTML)
        return
    badge = ctx.args[0].strip()
    try:
        today = date.today()
        punches = mdb_reader.get_employee_punches(badge, today, today)
        emps = mdb_reader.search_employee(badge)
        name = emps[0]['name'] if emps else badge
        if not punches:
            await update.message.reply_text(f'No punches today for {name} ({badge})')
            return
        lines = [f"🕐 <b>Punches Today — {name}</b>\n"]
        for i, p in enumerate(punches, 1):
            direction = '→ IN' if i % 2 == 1 else '← OUT'
            lines.append(f"{i}. {p['time']} {direction}")
        await update.message.reply_text('\n'.join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def cmd_employeereport(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage: /employeereport <badge>"""
    if not _allowed(update):
        return await _deny(update)
    if not ctx.args:
        await update.message.reply_text(
            'Usage: /employeereport &lt;badge&gt;\nExample: /employeereport 1024',
            parse_mode=ParseMode.HTML)
        return
    badge = ctx.args[0].strip()
    today = date.today()
    first_day = today.replace(day=1)
    await update.message.reply_text('⏳ Building employee report...')
    try:
        rep = mdb_reader.get_employee_report(badge, first_day, today)
        emp = rep['employee']
        lines = [
            f"👤 <b>Employee Report</b>",
            f"{emp['name']} ({emp['badge']}) — {emp['dept']}",
            f"📅 {first_day.strftime('%d/%m/%Y')} → {today.strftime('%d/%m/%Y')}",
            f"🕐 Shift start: {rep['shift_start']}",
            "",
            (f"✅ Present days: {rep['present_days']}  "
             f"❌ Absent days: {rep['absent_days']}  "
             f"⏰ Late days: {rep['late_days']}  🌅 Early days: {rep['early_days']}"),
            "",
            "<b>Recent days:</b>",
        ]
        recent_days = [d for d in rep['days'] if not d['is_weekend']][-10:]
        for d in recent_days:
            if d['punch_count'] == 0:
                lines.append(f"• {d['date'].strftime('%d/%m %a')}: ❌ Absent")
                continue
            if d['late_mins']:
                tag = '⏰'
            elif d['early_mins']:
                tag = '🌅'
            else:
                tag = '✅'
            lines.append(
                f"• {d['date'].strftime('%d/%m %a')}: {tag} "
                f"{d['first'] or '—'} → {d['last'] or '—'} ({d['punch_count']} punches)"
            )
        for chunk in _split('\n'.join(lines)):
            await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def cmd_calendar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage: /calendar <badge> [YYYY-MM]
    Without YYYY-MM → interactive date/range picker with inline keyboard.
    With YYYY-MM → static emoji-grid calendar (existing behaviour).
    """
    if not _allowed(update):
        return await _deny(update)
    if not ctx.args:
        await update.message.reply_text(
            'Usage:\n'
            '  /calendar &lt;badge&gt; — interactive date/range picker\n'
            '  /calendar &lt;badge&gt; YYYY-MM — static monthly calendar\n'
            'Example: /calendar 1024', parse_mode=ParseMode.HTML)
        return
    badge = ctx.args[0].strip()

    # ── Static monthly calendar (legacy, with explicit month) ──────────
    if len(ctx.args) > 1:
        month_str = ctx.args[1]
        try:
            year, month = map(int, month_str.split('-'))
        except ValueError:
            await update.message.reply_text('❌ Month format: YYYY-MM')
            return
        await update.message.reply_text('⏳ Building calendar...')
        try:
            cal  = mdb_reader.get_employee_calendar(badge, year, month)
            text = _fmt_calendar(cal)
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        except Exception as e:
            await update.message.reply_text(f'❌ {e}')
        return

    # ── Interactive calendar picker ─────────────────────────────────────
    try:
        emps = mdb_reader.search_employee(badge)
        if not emps:
            await update.message.reply_text(f'❌ No employee found for badge "{badge}"')
            return
        emp  = emps[0]
        name = emp['name']
        dept = emp['dept']
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')
        return

    chat_id = str(update.effective_chat.id)
    _cal_state[chat_id] = {'badge': badge, 'name': name, 'dept': dept}

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton('📅 Single Date', callback_data=f'att_mode:S:{badge}'),
            InlineKeyboardButton('📆 Date Range',  callback_data=f'att_mode:F:{badge}'),
        ],
        [InlineKeyboardButton('❌ Cancel', callback_data='att_cancel')],
    ])
    await update.message.reply_text(
        f'👤 <b>{name}</b> ({badge}) — {dept}\nChoose a date option:',
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )

async def callback_calendar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle all inline keyboard callbacks for the interactive calendar."""
    query   = update.callback_query
    await query.answer()

    if not _allowed(update):
        return

    data    = query.data or ''
    chat_id = str(update.effective_chat.id)

    # ── No-op (header labels, empty cells) ─────────────────────────────
    if data == 'cal_noop':
        return

    # ── Cancel ──────────────────────────────────────────────────────────
    if data == 'att_cancel':
        _cal_state.pop(chat_id, None)
        await query.edit_message_text('❌ Cancelled.')
        return

    # ── Report date selection ────────────────────────────────────────────
    if data.startswith('rep:'):
        action = data[4:]
        if action == 'cancel':
            await query.edit_message_text('❌ Cancelled.')
            return
        if action == 'today':
            report_date = date.today()
        elif action == 'yesterday':
            report_date = date.today() - timedelta(days=1)
        elif action == 'pick':
            today = date.today()
            kb = _make_cal_keyboard(today.year, today.month, 'R', '_rep_')
            await query.edit_message_text(
                '📊 <b>Absent Report</b>\n\n📅 Select date:',
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
            return
        elif action == 'range':
            today = date.today()
            kb = _make_cal_keyboard(today.year, today.month, 'RF', '_rep_')
            await query.edit_message_text(
                '📊 <b>Absent Report — Date Range</b>\n\n📅 Select <b>start</b> date:',
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
            return
        else:
            return
        await query.edit_message_text(
            f'⏳ Building report for {report_date.strftime("%d/%m/%Y")}…')
        await _send_absent_report_callback(query, update, report_date)
        return

    # ── Mail date selection ──────────────────────────────────────────────
    if data.startswith('mail:'):
        action = data[5:]
        if action == 'cancel':
            await query.edit_message_text('❌ Cancelled.')
            return
        if action == 'today':
            await _send_email_for_date(query, update, date.today())
            return
        if action == 'pick':
            today = date.today()
            kb = _make_cal_keyboard(today.year, today.month, 'M', '_mail_')
            await query.edit_message_text(
                '📧 <b>Email Attendance Report</b>\n\n📅 Select date:',
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
            return
        return

    # ── Mode selection (Single / Range-start) ───────────────────────────
    if data.startswith('att_mode:'):
        # format: att_mode:{S|F}:{badge}
        parts = data.split(':', 2)
        if len(parts) < 3:
            return
        mode  = parts[1]
        badge = parts[2]
        st    = _cal_state.get(chat_id, {})
        name  = st.get('name', badge)
        today = date.today()
        kb    = _make_cal_keyboard(today.year, today.month, mode, badge)
        title = (f'📅 <b>Select date</b>\n👤 {name} ({badge})'
                 if mode == 'S' else
                 f'📅 <b>Select start date</b>\n👤 {name} ({badge})')
        await query.edit_message_text(title, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    # ── Month navigation ─────────────────────────────────────────────────
    if data.startswith('cal_nav:'):
        # format: cal_nav:{year}:{month}:{mode}:{badge}:{range_from}
        parts = data.split(':')
        if len(parts) < 6:
            return
        try:
            year  = int(parts[1])
            month = int(parts[2])
        except ValueError:
            return
        mode       = parts[3]
        badge      = parts[4]
        range_from = parts[5] if len(parts) > 5 else ''
        st   = _cal_state.get(chat_id, {})
        name = st.get('name', badge)
        kb   = _make_cal_keyboard(year, month, mode, badge, range_from)
        if mode == 'S':
            title = f'📅 <b>Select date</b>\n👤 {name} ({badge})'
        elif mode == 'F':
            title = f'📅 <b>Select start date</b>\n👤 {name} ({badge})'
        elif mode == 'R':
            title = '📊 <b>Absent Report</b>\n\n📅 Select date:'
        elif mode == 'RF':
            title = '📊 <b>Absent Report — Date Range</b>\n\n📅 Select <b>start</b> date:'
        elif mode == 'RT':
            title = (f'📊 <b>Absent Report — Date Range</b>\n\n'
                     f'📅 Select <b>end</b> date:\n⏩ From: {range_from}')
        else:
            title = (f'📅 <b>Select end date</b>\n👤 {name} ({badge})\n'
                     f'⏩ From: {range_from}')
        await query.edit_message_text(title, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    # ── Day selection ────────────────────────────────────────────────────
    if data.startswith('cal_day:'):
        # format: cal_day:{YYYY-MM-DD}:{mode}:{badge}:{range_from}
        parts = data.split(':')
        if len(parts) < 5:
            return
        selected_date = parts[1]
        mode          = parts[2]
        badge         = parts[3]
        range_from    = parts[4] if len(parts) > 4 else ''
        st   = _cal_state.get(chat_id, {})
        name = st.get('name', badge)
        dept = st.get('dept', '')

        if mode == 'S':
            # Single day — fetch and display
            _cal_state.pop(chat_id, None)
            await query.edit_message_text(
                f'⏳ Fetching attendance for {name} — {selected_date}…',
                parse_mode=ParseMode.HTML)
            result = _fmt_day_punches(badge, name, dept, selected_date)
            await _edit_or_followup(query, update, result)

        elif mode == 'F':
            # Got range start — show calendar for end date
            year  = int(selected_date[:4])
            month = int(selected_date[5:7])
            _cal_state[chat_id] = {**st, 'range_from': selected_date}
            kb    = _make_cal_keyboard(year, month, 'T', badge, selected_date)
            title = (f'📅 <b>Select end date</b>\n👤 {name} ({badge})\n'
                     f'⏩ From: {selected_date}')
            await query.edit_message_text(title, parse_mode=ParseMode.HTML, reply_markup=kb)

        elif mode == 'T':
            # Got range end — fetch and display
            _cal_state.pop(chat_id, None)
            d_from_str = range_from
            d_to_str   = selected_date
            try:
                if (datetime.strptime(d_from_str, '%Y-%m-%d') >
                        datetime.strptime(d_to_str, '%Y-%m-%d')):
                    d_from_str, d_to_str = d_to_str, d_from_str
            except ValueError:
                pass
            await query.edit_message_text(
                f'⏳ Fetching attendance for {name} — {d_from_str} to {d_to_str}…',
                parse_mode=ParseMode.HTML)
            result = _fmt_range_punches(badge, name, dept, d_from_str, d_to_str)
            await _edit_or_followup(query, update, result)

        elif mode == 'R':
            # Report date selected from calendar picker
            report_date = datetime.strptime(selected_date, '%Y-%m-%d').date()
            await query.edit_message_text(
                f'⏳ Building report for {report_date.strftime("%d/%m/%Y")}…')
            await _send_absent_report_callback(query, update, report_date)

        elif mode == 'RF':
            # Report range — start date selected; show calendar for end date
            year  = int(selected_date[:4])
            month = int(selected_date[5:7])
            kb    = _make_cal_keyboard(year, month, 'RT', '_rep_', selected_date)
            await query.edit_message_text(
                f'📊 <b>Absent Report — Date Range</b>\n\n'
                f'📅 Select <b>end</b> date:\n⏩ From: {selected_date}',
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )

        elif mode == 'RT':
            # Report range — end date selected; build reports for the whole range
            d_from_str = range_from
            d_to_str   = selected_date
            try:
                if (datetime.strptime(d_from_str, '%Y-%m-%d') >
                        datetime.strptime(d_to_str, '%Y-%m-%d')):
                    d_from_str, d_to_str = d_to_str, d_from_str
            except ValueError:
                pass
            await _send_range_absent_reports_callback(query, update, d_from_str, d_to_str)

        elif mode == 'M':
            # Mail — date selected from calendar picker; send email for that date
            report_date = datetime.strptime(selected_date, '%Y-%m-%d').date()
            await _send_email_for_date(query, update, report_date)
        return

async def _edit_or_followup(query, update: Update, text: str):
    """Edit the callback message with text; if too long, send follow-up chunks."""
    chunks = _split(text)
    await query.edit_message_text(chunks[0], parse_mode=ParseMode.HTML)
    for chunk in chunks[1:]:
        await update.effective_chat.send_message(chunk, parse_mode=ParseMode.HTML)

# ── Device commands ───────────────────────────────────────────────────────────

def _get_device_by_index(idx: int) -> dict:
    devices = settings.get_devices()
    if idx < 0 or idx >= len(devices):
        raise IndexError('Device no longer exists. Refresh the panel and try again.')
    return devices[idx]


def _validate_device_ip(ip_text: str, devices: list, exclude_index: int = None) -> str:
    ip = ip_text.strip()
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        raise ValueError('Please send a valid IPv4/IPv6 address.')
    for i, dev in enumerate(devices):
        if exclude_index is not None and i == exclude_index:
            continue
        if dev['ip'] == ip:
            raise ValueError(f"Device IP '{ip}' already exists.")
    return ip


def _validate_device_name(name_text: str, devices: list, exclude_index: int = None) -> str:
    if not name_text.strip():
        raise ValueError('Device name cannot be empty.')
    name = _normalize_whitespace(name_text)
    for i, dev in enumerate(devices):
        if exclude_index is not None and i == exclude_index:
            continue
        existing = _normalize_whitespace(str(dev.get('name', '')))
        if existing.lower() == name.lower():
            raise ValueError(f"Device name '{name}' conflicts with existing device '{existing}'.")
    return name


def _validate_device_port(port_text: str) -> int:
    try:
        port = int(port_text.strip())
    except ValueError:
        raise ValueError('Port must be a number between 1 and 65535.')
    if not (1 <= port <= 65535):
        raise ValueError('Port must be a number between 1 and 65535.')
    return port


def _normalize_whitespace(value: str) -> str:
    return ' '.join(value.strip().split())


async def _send_device_panel(chat, prefix: str = ''):
    statuses = zk_devices.get_device_status()
    text = _device_panel_text(statuses)
    if prefix:
        text = prefix + '\n\n' + text
    await chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=_device_panel_kb(statuses))


async def cmd_device(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    await update.message.reply_text('⏳ Loading device admin panel...')
    try:
        statuses = zk_devices.get_device_status()
        _audit('device.panel.view', update, f'count={len(statuses)}')
        await update.message.reply_text(
            _device_panel_text(statuses),
            parse_mode=ParseMode.HTML,
            reply_markup=_device_panel_kb(statuses),
        )
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')


async def callback_device(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _allowed(update):
        return
    data = query.data or ''
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    parts = data.split(':')
    action = parts[1] if len(parts) > 1 else ''

    if action == 'cancel':
        _device_state.pop(chat_id, None)
        await query.edit_message_text('❌ Device action cancelled.')
        return

    if action == 'refresh':
        statuses = zk_devices.get_device_status()
        await query.edit_message_text(
            _device_panel_text(statuses),
            parse_mode=ParseMode.HTML,
            reply_markup=_device_panel_kb(statuses),
        )
        return

    if action == 'add':
        _device_state[chat_id] = {
            'user_id': user_id,
            'action': 'add',
            'step': 'ip',
            'draft': {},
        }
        await query.message.reply_text(
            '➕ <b>Add Device</b>\n\nSend the new device IP address.',
            parse_mode=ParseMode.HTML,
            reply_markup=_device_cancel_kb(),
        )
        return

    if action == 'edit' and len(parts) >= 3:
        try:
            idx = int(parts[2])
        except ValueError:
            await query.message.reply_text('❌ Invalid device reference. Refresh /device and try again.')
            return
        try:
            dev = _get_device_by_index(idx)
        except Exception as exc:
            await query.message.reply_text(f'❌ {exc}')
            return
        await query.message.reply_text(
            f'✏️ <b>Edit Device</b>\n\n<b>{html.escape(dev["name"])}</b>\n'
            f'<code>{html.escape(dev["ip"])}:{dev["port"]}</code>\n\nChoose what to update:',
            parse_mode=ParseMode.HTML,
            reply_markup=_device_action_kb(idx),
        )
        return

    if action == 'edit_field' and len(parts) >= 4:
        try:
            idx = int(parts[2])
        except ValueError:
            await query.message.reply_text('❌ Invalid device reference. Refresh /device and try again.')
            return
        field = parts[3]
        try:
            dev = _get_device_by_index(idx)
        except Exception as exc:
            await query.message.reply_text(f'❌ {exc}')
            return
        _device_state[chat_id] = {
            'user_id': user_id,
            'action': 'edit',
            'step': field,
            'device_index': idx,
        }
        prompts = {
            'ip': f'🌐 Send new IP for <b>{html.escape(dev["name"])}</b>.\nCurrent: <code>{html.escape(dev["ip"])}</code>',
            'name': f'🏷 Send new name for <b>{html.escape(dev["name"])}</b>.',
            'port': f'🔌 Send new port for <b>{html.escape(dev["name"])}</b>.\nCurrent: <code>{dev["port"]}</code>',
        }
        await query.message.reply_text(
            prompts.get(field, 'Send the new value.'),
            parse_mode=ParseMode.HTML,
            reply_markup=_device_cancel_kb(),
        )
        return

    if action == 'rename' and len(parts) >= 3:
        try:
            idx = int(parts[2])
        except ValueError:
            await query.message.reply_text('❌ Invalid device reference. Refresh /device and try again.')
            return
        try:
            dev = _get_device_by_index(idx)
        except Exception as exc:
            await query.message.reply_text(f'❌ {exc}')
            return
        _device_state[chat_id] = {
            'user_id': user_id,
            'action': 'rename',
            'step': 'name',
            'device_index': idx,
        }
        await query.message.reply_text(
            f'🏷 <b>Rename Device</b>\n\nSend a new display name for <b>{html.escape(dev["name"])}</b>.',
            parse_mode=ParseMode.HTML,
            reply_markup=_device_cancel_kb(),
        )
        return

    if action == 'remove' and len(parts) >= 3:
        try:
            idx = int(parts[2])
        except ValueError:
            await query.message.reply_text('❌ Invalid device reference. Refresh /device and try again.')
            return
        try:
            dev = _get_device_by_index(idx)
        except Exception as exc:
            await query.message.reply_text(f'❌ {exc}')
            return
        await query.message.reply_text(
            f'❓ Remove <b>{html.escape(dev["name"])}</b>\n'
            f'<code>{html.escape(dev["ip"])}:{dev["port"]}</code> from <code>[devices]</code>?',
            parse_mode=ParseMode.HTML,
            reply_markup=_device_remove_kb(idx),
        )
        return

    if action == 'confirm_remove' and len(parts) >= 3:
        try:
            idx = int(parts[2])
        except ValueError:
            await query.edit_message_text('❌ Invalid device reference. Refresh /device and try again.')
            return
        devices = settings.get_devices()
        if idx < 0 or idx >= len(devices):
            await query.edit_message_text('❌ Device no longer exists. Refresh /device and try again.')
            return
        removed = devices.pop(idx)
        settings.save_devices(devices)
        _device_state.pop(chat_id, None)
        _audit('device.remove', update,
               f'name={removed["name"]} ip={removed["ip"]} port={removed["port"]}')
        await query.edit_message_text(
            f'✅ Removed <b>{html.escape(removed["name"])}</b> '
            f'(<code>{html.escape(removed["ip"])}:{removed["port"]}</code>) from config.',
            parse_mode=ParseMode.HTML,
        )
        await _send_device_panel(
            update.effective_chat,
            prefix=f'✅ Device removed by {_safe_display_name(update)}.',
        )
        return

async def cmd_devices(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    await update.message.reply_text('⏳ Pinging devices...')
    try:
        statuses = zk_devices.get_device_status()
        await update.message.reply_text(_fmt_devices(statuses),
                                        parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def cmd_clocksync(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    await update.message.reply_text('⏳ Syncing clocks...')
    try:
        results = zk_devices.sync_clocks()
        lines = ['🕐 <b>Clock Sync Results</b>\n']
        for r in results:
            icon = '✅' if r['ok'] else '❌'
            line = f"{icon} {r['name']} ({r['ip']})"
            if r['ok']:
                line += f" — set to {r.get('time_set','')}"
            else:
                line += f" — {r.get('error','')}"
            lines.append(line)
        await update.message.reply_text('\n'.join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def cmd_reboot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage: /reboot <ip or device name> | /reboot all"""
    if not _allowed(update):
        return await _deny(update)
    if not ctx.args:
        await update.message.reply_text(
            'Usage: /reboot &lt;ip or name&gt;\n'
            'Example: /reboot 10.20.141.23\n'
            'Example: /reboot Boys 1\n'
            'Example: /reboot all', parse_mode=ParseMode.HTML)
        return
    target = ' '.join(ctx.args).strip()
    if target.lower() == 'all':
        await update.message.reply_text('⏳ Rebooting all devices...')
        try:
            results = zk_devices.reboot_all()
            lines = ['🔄 <b>Reboot All Results</b>\n']
            for r in results:
                icon = '✅' if r['ok'] else '❌'
                line = f"{icon} {r['name']} ({r['ip']})"
                if not r['ok']:
                    line += f" — {r.get('error', 'Unknown error')}"
                lines.append(line)
            await update.message.reply_text('\n'.join(lines), parse_mode=ParseMode.HTML)
        except Exception as e:
            await update.message.reply_text(f'❌ {e}')
        return
    await update.message.reply_text(f'⏳ Rebooting {target}...')
    try:
        result = zk_devices.reboot_device(target)
        if result['ok']:
            await update.message.reply_text(
                f"✅ Rebooted: {result['name']} ({result['ip']})")
        else:
            await update.message.reply_text(
                f"❌ Reboot failed: {result.get('error','Unknown error')}")
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def cmd_usersync(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    await update.message.reply_text('⏳ Syncing users across devices...')
    try:
        results = zk_devices.sync_users()
        lines = ['👤 <b>User Sync Results</b>\n']
        for r in results:
            icon = '✅' if r['ok'] else '❌'
            line = f"{icon} {r['name']} ({r['ip']})"
            if r['ok']:
                pushed = r.get('pushed', 0)
                line += f" — pushed {pushed} users" if pushed else f" — {r.get('note','in sync')}"
            else:
                line += f" — {r.get('error','')}"
            lines.append(line)
        await update.message.reply_text('\n'.join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def cmd_adduser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage: /adduser <badge> <full name>"""
    if not _allowed(update):
        return await _deny(update)
    if len(ctx.args) < 2:
        await update.message.reply_text(
            'Usage: /adduser &lt;badge&gt; &lt;full name&gt;\n'
            'Example: /adduser 1500 AHMED AL RASHID\n\n'
            '⚠️ Biometric enrollment must be done on the device physically.\n'
            'Middle East Attendance Software will sync the user on next download.',
            parse_mode=ParseMode.HTML)
        return
    badge = ctx.args[0].strip()
    name  = ' '.join(ctx.args[1:]).strip().upper()
    await update.message.reply_text(f'⏳ Adding {name} ({badge}) to all devices...')
    try:
        results = zk_devices.add_user(badge, name)
        lines = [f"👤 <b>Add User: {name} [{badge}]</b>\n"]
        for r in results:
            icon = '✅' if r['ok'] else '❌'
            line = f"{icon} {r['name']} ({r['ip']})"
            if not r['ok']:
                line += f" — {r.get('error','')}"
            lines.append(line)
        lines.append('\n⚠️ Enroll biometrics on device physically.')
        await update.message.reply_text('\n'.join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def cmd_unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    await update.message.reply_text('⏳ Checking devices for unknown users...')
    try:
        emps = mdb_reader.get_employees(active_only=False)
        known = {e['badge'] for e in emps}
        unknown = zk_devices.get_unknown_users(known)
        if not unknown:
            await update.message.reply_text('✅ All device users are mapped in MDB.')
            return
        lines = [f"⚠️ <b>Unknown Users ({len(unknown)})</b>\n"]
        for u in unknown:
            lines.append(
                f"🔴 UID:{u['uid']} | ID:{u['user_id']} | "
                f"Name: {u['name_on_device'] or '—'}\n"
                f"   Device: {u['device_name']} ({u['device_ip']})")
        for chunk in _split('\n'.join(lines)):
            await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

# ── Database commands ─────────────────────────────────────────────────────────

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    try:
        s = mdb_reader.get_db_stats()
        icon = '🟢' if s['accessible'] else '🔴'
        text = (
            f"🗄️ <b>MDB Stats</b>\n\n"
            f"{icon} Accessible: {'Yes' if s['accessible'] else 'No'}\n"
            f"📁 Size: {s['size_mb']} MB\n"
            f"🕐 Modified: {s['last_modified']}\n"
            f"👥 Employees: {s['total_employees']} total / {s['active_employees']} active\n"
            f"📂 Path: <code>{s['mdb_path']}</code>"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def cmd_mdbinfo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    try:
        info = mdb_reader.get_mdb_info()
        icon = '🟢' if info['accessible'] else '🔴'
        text = (
            f"📂 <b>MDB Info</b>\n\n"
            f"Configured path:\n<code>{info['configured_path']}</code>\n\n"
            f"Local path:\n<code>{info['local_path']}</code>\n\n"
            f"{icon} Accessible: {'Yes' if info['accessible'] else 'No'}\n"
            f"📦 Size: {info['size_mb']} MB\n"
            f"🕐 Last modified: {info['last_modified']}\n\n"
            f"To change: /setmdb &lt;new path&gt;"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def cmd_setmdb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage: /setmdb <path>"""
    if not _allowed(update):
        return await _deny(update)
    if not ctx.args:
        await update.message.reply_text(
            'Usage: /setmdb &lt;path&gt;\n\n'
            'Examples:\n'
            '<code>/setmdb //10.20.141.17/d/Attendance database/attBackup23.12.25.mdb</code>\n'
            '<code>/setmdb /mnt/attdb/att.mdb</code>\n'
            '<code>/setmdb //192.168.1.100/share/att.mdb</code>',
            parse_mode=ParseMode.HTML)
        return
    new_path = ' '.join(ctx.args).strip()
    try:
        mdb_reader.set_mdb_path(new_path)
        # Test accessibility
        info = mdb_reader.get_mdb_info()
        icon = '🟢' if info['accessible'] else '🟡'
        status = 'Accessible ✅' if info['accessible'] else 'Not yet accessible — check mount/path ⚠️'
        await update.message.reply_text(
            f"{icon} <b>MDB Path Updated</b>\n\n"
            f"<code>{new_path}</code>\n\n"
            f"Status: {status}",
            parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def cmd_tables(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return await _deny(update)
    try:
        tables = mdb_reader.list_tables()
        text = '📋 <b>MDB Tables</b>\n\n' + '\n'.join(f'• {t}' for t in tables)
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def cmd_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage: /download <ip> (read-only adaptation)."""
    if not _allowed(update):
        return await _deny(update)
    if not ctx.args:
        await update.message.reply_text(
            'Usage: /download &lt;ip&gt;\n'
            'Read-only adaptation: device snapshot only (no MDB write).',
            parse_mode=ParseMode.HTML)
        return
    ip = ctx.args[0].strip()
    await update.message.reply_text(f'⏳ Read-only snapshot for device {ip}...')
    try:
        dev = zk_devices.get_device_by_ip(ip)
        statuses = zk_devices.get_device_status()
        status = next((s for s in statuses if s.get('ip') == ip), None)
        candidates = [ip]
        if '.' in ip:
            candidates.append(ip.split('.')[-1])
        punches = []
        for cand in candidates:
            punches = mdb_reader.get_sensor_punches(cand, n=10, days_back=7)
            if punches:
                break
        lines = [
            f"📥 <b>/download {ip} (Read-Only)</b>",
            "No data was written to MDB.",
            "",
            f"Device in config: {'Yes' if dev else 'No'}",
        ]
        if status:
            lines.append(f"Status: {'🟢 Online' if status.get('online') else '🔴 Offline'}")
            if status.get('time'):
                lines.append(f"Device time: {status['time']}")
            if status.get('users') is not None:
                lines.append(f"Users: {status['users']}")
        if punches:
            lines.append("")
            lines.append("<b>Latest MDB punches linked to this device id:</b>")
            for p in punches:
                p_date_obj = p.get('date')
                p_date = p_date_obj.strftime('%d/%m') if isinstance(p_date_obj, date) else str(p_date_obj or '')
                lines.append(
                    f"• {p_date} {p['time']} — {p['name']} ({p['badge']})"
                )
        else:
            lines.append("\nNo recent MDB punches matched this device id.")
        for chunk in _split('\n'.join(lines)):
            await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

# ── /dbbackup helpers ─────────────────────────────────────────────────────────

_TG_MAX_DOC_BYTES = 49 * 1024 * 1024   # 49 MB safe Telegram document limit

_BACKUP_METHOD_LABELS = {
    'tg':   '📱 Telegram',
    'mail': '📧 Mail',
    'copy': '📁 Copy',
    'mc':   '📧📁 Mail + Copy',
    'tm':   '📱📧 Telegram + Mail',
    'tc':   '📱📁 Telegram + Copy',
    'all':  '🔀 All (Telegram + Mail + Copy)',
}

_BACKUP_FREQ_LABELS = {
    'daily':  'Daily',
    'weekly': 'Weekly (configured days)',
}


def _backup_method_label(method: str) -> str:
    return _BACKUP_METHOD_LABELS.get(method, method.upper())


def _get_mdb_size_info() -> tuple:
    """Return (local_path, size_bytes, accessible). Never raises."""
    try:
        info = mdb_reader.get_mdb_info()
        local_path = info.get('local_path', '')
        accessible = bool(info.get('accessible') and local_path and os.path.isfile(local_path))
        size_bytes = os.path.getsize(local_path) if accessible else 0
        return local_path, size_bytes, accessible
    except Exception:
        return '', 0, False


def _fmt_backup_panel() -> tuple:
    """Return (text, InlineKeyboardMarkup) for the /dbbackup main panel."""
    local_path, size_bytes, accessible = _get_mdb_size_info()
    fname    = os.path.basename(local_path) if local_path else '—'
    size_str = f"{size_bytes / (1024 * 1024):.1f} MB" if accessible else '—'
    tg_ok    = accessible and size_bytes <= _TG_MAX_DOC_BYTES

    sched_on  = settings.get_backup_enabled()
    sched_ico = '🟢' if sched_on else '🔴'
    method    = _backup_method_label(settings.get_backup_method())

    text = (
        f"📦 <b>MDB Backup</b>\n\n"
        f"📄 File: <code>{fname}</code>\n"
        f"💾 Size: <b>{size_str}</b>\n"
        f"{'✅ Accessible' if accessible else '❌ Not accessible'}\n\n"
        f"{sched_ico} Schedule: <b>{'ENABLED' if sched_on else 'DISABLED'}</b>"
        f"  ({settings.get_backup_schedule().title()} at {settings.backup_time_label()})\n"
        f"📬 Method: <b>{method}</b>"
    )

    rows: list = []
    if tg_ok:
        rows.append([InlineKeyboardButton('📱 Telegram Download', callback_data='bk:dl')])
    elif accessible and not tg_ok:
        text += f'\n\n⚠️ File too large for Telegram ({size_bytes / (1024 * 1024):.1f} MB > 49 MB). Use Mail or Copy.'
    rows.append([
        InlineKeyboardButton('📧 Mail Backup',   callback_data='bk:mail'),
        InlineKeyboardButton('📁 Copy to Dir',   callback_data='bk:copy'),
    ])
    rows.append([InlineKeyboardButton('⚙️ Schedule Settings', callback_data='bk:sched')])
    rows.append([InlineKeyboardButton('✅ Done',               callback_data='bk:done')])
    return text, InlineKeyboardMarkup(rows)


def _fmt_backup_sched_panel() -> tuple:
    """Return (text, kb) for the backup schedule settings sub-panel."""
    on       = settings.get_backup_enabled()
    sched    = settings.get_backup_schedule()
    freq_lbl = _BACKUP_FREQ_LABELS.get(sched, sched.title())
    method   = _backup_method_label(settings.get_backup_method())
    copy_dir = settings.get_backup_copy_dir() or '—'
    ico      = '🟢' if on else '🔴'

    text = (
        f"⚙️ <b>Backup Schedule Settings</b>\n\n"
        f"{ico} Schedule: <b>{'ENABLED' if on else 'DISABLED'}</b>\n"
        f"🔄 Frequency: <b>{freq_lbl}</b>\n"
        f"🕐 Time: <b>{settings.backup_time_label()}</b>\n"
        f"📅 Days: <b>{settings.backup_days_label()}</b>\n"
        f"📬 Method: <b>{method}</b>\n"
        f"📁 Copy Dir: <code>{copy_dir}</code>"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f'{ico} {"On" if on else "Off"}', callback_data='bk:stoggle'),
            InlineKeyboardButton('🔄 Frequency',                   callback_data='bk:sfreq'),
        ],
        [
            InlineKeyboardButton('🕐 Time',      callback_data='bk:stime'),
            InlineKeyboardButton('📅 Days',      callback_data='bk:sdays'),
        ],
        [
            InlineKeyboardButton('📬 Method',    callback_data='bk:smeth'),
            InlineKeyboardButton('📁 Copy Dir',  callback_data='bk:scopy'),
        ],
        [InlineKeyboardButton('📧 Backup Mail Settings', callback_data='bk:bkmail')],
        [
            InlineKeyboardButton('← Back',       callback_data='bk:sback'),
            InlineKeyboardButton('✅ Done',       callback_data='bk:sdone'),
        ],
    ])
    return text, kb


def _fmt_backup_mail_panel() -> tuple:
    """Return (text, kb) for backup-specific mail settings."""
    sender_raw = settings.get_backup_sender_email_raw()
    sender_disp = sender_raw or f'(smtp fallback: {settings.get_smtp_sender_email() or "—"})'
    name_raw    = settings.get_backup_sender_name_raw()
    name_disp   = name_raw or f'(smtp fallback: {settings.get_smtp_sender_name() or "—"})'
    pass_raw    = settings.get_backup_app_password_raw()
    if pass_raw:
        pass_disp = '✅ Set (backup-specific)'
    elif settings.get_smtp_app_password():
        pass_disp = '(smtp fallback: ✅ Set)'
    else:
        pass_disp = '❌ Not set'
    recip_raw = settings.get_backup_recipients_raw()
    if recip_raw:
        recip_disp = f'{len(recip_raw)} custom address(es)'
    else:
        n = len(settings.get_smtp_recipients())
        recip_disp = f'(smtp fallback: {n} address(es))'

    text = (
        f"📧 <b>Backup Mail Settings</b>\n\n"
        f"<i>Leave fields blank to fall back to [smtp] report-mail settings.</i>\n\n"
        f"📤 Sender: <code>{sender_disp}</code>\n"
        f"👤 Name: <b>{name_disp}</b>\n"
        f"🔑 Password: <b>{pass_disp}</b>\n"
        f"👥 Recipients: <b>{recip_disp}</b>"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton('📤 Sender',     callback_data='bk:msender'),
            InlineKeyboardButton('👤 Name',        callback_data='bk:mname'),
        ],
        [
            InlineKeyboardButton('🔑 Password',   callback_data='bk:mpass'),
            InlineKeyboardButton('👥 Recipients', callback_data='bk:mrecip'),
        ],
        [InlineKeyboardButton('← Back', callback_data='bk:mback')],
    ])
    return text, kb


def _backup_recipients_menu_kb() -> InlineKeyboardMarkup:
    recipients = settings.get_backup_recipients_raw()
    rows = []
    for i, addr in enumerate(recipients):
        rows.append([InlineKeyboardButton(f'❌ {addr}', callback_data=f'bk:mrm:{i}')])
    rows.append([InlineKeyboardButton('➕ Add Recipient', callback_data='bk:madd')])
    rows.append([InlineKeyboardButton('← Back', callback_data='bk:mback')])
    return InlineKeyboardMarkup(rows)


def _backup_recipients_text() -> str:
    recipients = settings.get_backup_recipients_raw()
    if not recipients:
        smtp_n = len(settings.get_smtp_recipients())
        return (
            f'👥 <b>Backup Recipients</b>\n\n'
            f'No custom recipients — using smtp fallback ({smtp_n} address(es)).\n\n'
            'Add a custom address to override:'
        )
    addr_list = '\n'.join(f'  {i + 1}. {r}' for i, r in enumerate(recipients))
    return (
        f'👥 <b>Backup Recipients ({len(recipients)})</b>\n\n{addr_list}\n\n'
        'Tap ❌ to remove, or add a new address:'
    )


def _backup_freq_menu_kb() -> InlineKeyboardMarkup:
    current = settings.get_backup_schedule()
    rows = []
    for val, label in _BACKUP_FREQ_LABELS.items():
        checked = val == current
        rows.append([InlineKeyboardButton(
            f"{'✅' if checked else '⬜'} {label}",
            callback_data=f'bk:sfr:{val}',
        )])
    rows.append([InlineKeyboardButton('← Back', callback_data='bk:sback')])
    return InlineKeyboardMarkup(rows)


def _backup_method_menu_kb() -> InlineKeyboardMarkup:
    current = settings.get_backup_method()
    rows = []
    for val, label in _BACKUP_METHOD_LABELS.items():
        checked = val == current
        rows.append([InlineKeyboardButton(
            f"{'✅' if checked else '⬜'} {label}",
            callback_data=f'bk:smeth:{val}',
        )])
    rows.append([InlineKeyboardButton('← Back', callback_data='bk:sback')])
    return InlineKeyboardMarkup(rows)


def _backup_days_menu_kb() -> InlineKeyboardMarkup:
    current = settings.get_backup_days()
    selected = {int(d.strip()) for d in current.split(',') if d.strip().isdigit()}
    btns = []
    for day_num, day_name in _DAY_NAMES.items():
        checked = day_num in selected
        btns.append(InlineKeyboardButton(
            f"{'✅' if checked else '⬜'} {day_name}",
            callback_data=f'bk:sday:{day_num}',
        ))
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    rows.append([InlineKeyboardButton('← Back', callback_data='bk:sback')])
    return InlineKeyboardMarkup(rows)


# ── /dbbackup command ─────────────────────────────────────────────────────────

async def cmd_dbbackup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """MDB Backup panel — inline keyboard with action choices."""
    if not _allowed(update):
        return await _deny(update)
    _audit('dbbackup.open', update)
    text, kb = _fmt_backup_panel()
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


# ── /dbbackup callback handler ────────────────────────────────────────────────

async def callback_dbbackup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle all inline keyboard callbacks for /dbbackup (prefix 'bk:')."""
    query   = update.callback_query
    await query.answer()
    if not _allowed(update):
        return
    data    = query.data or ''
    chat_id = str(update.effective_chat.id)
    action  = data[3:]   # strip 'bk:'

    # ── Main panel ────────────────────────────────────────────────────────────

    if action == 'done':
        _bk_state.pop(chat_id, None)
        await query.edit_message_text('✅ Backup panel closed.')
        return

    if action == 'back':
        _bk_state.pop(chat_id, None)
        text, kb = _fmt_backup_panel()
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if action == 'dl':
        local_path, size_bytes, accessible = _get_mdb_size_info()
        if not accessible:
            await query.edit_message_text('❌ MDB is not accessible. Check /mdbinfo.')
            return
        if size_bytes > _TG_MAX_DOC_BYTES:
            await query.edit_message_text(
                f'❌ File is {size_bytes / (1024 * 1024):.1f} MB — too large for Telegram.\n'
                'Use 📧 Mail or 📁 Copy instead.'
            )
            return
        await query.edit_message_text('⏳ Sending MDB to Telegram…')
        try:
            stamp    = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"mdb_backup_{stamp}.mdb"
            with open(local_path, 'rb') as fh:
                await query.message.reply_document(
                    document=fh,
                    filename=filename,
                    caption='📦 MDB backup (manual Telegram download)',
                )
            _audit('dbbackup.tg', update, f'file={filename} size={size_bytes}')
            await query.edit_message_text(f'✅ MDB sent as <code>{filename}</code>.', parse_mode=ParseMode.HTML)
        except Exception as e:
            _audit('dbbackup.tg.error', update, str(e))
            await query.edit_message_text(f'❌ Telegram send failed: {e}')
        return

    if action == 'mail':
        local_path, size_bytes, accessible = _get_mdb_size_info()
        if not accessible:
            await query.edit_message_text('❌ MDB is not accessible. Check /mdbinfo.')
            return
        missing = []
        if not settings.get_backup_sender_email():
            missing.append('sender email')
        if not settings.get_backup_app_password():
            missing.append('app password')
        if not settings.get_backup_recipients():
            missing.append('recipients')
        if missing:
            await query.edit_message_text(
                f'❌ Backup mail not fully configured. Missing: {", ".join(missing)}.\n'
                'Use ⚙️ Schedule Settings → 📧 Backup Mail Settings to configure.'
            )
            return
        await query.edit_message_text('⏳ Mailing MDB backup…')
        stamp    = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"mdb_backup_{stamp}.mdb"
        ok, err  = email_sender.send_backup_email(
            sender_email=settings.get_backup_sender_email(),
            sender_name=settings.get_backup_sender_name(),
            app_password=settings.get_backup_app_password(),
            recipients=settings.get_backup_recipients(),
            mdb_path=local_path,
            filename=filename,
        )
        n = len(settings.get_backup_recipients())
        if ok:
            _audit('dbbackup.mail', update, f'file={filename} recipients={n}')
            await query.edit_message_text(
                f'✅ MDB backup mailed to {n} recipient(s).\n'
                f'File: <code>{filename}</code>', parse_mode=ParseMode.HTML
            )
        else:
            _audit('dbbackup.mail.error', update, err[:200])
            await query.edit_message_text(f'❌ Mail failed: {err}')
        return

    if action == 'copy':
        copy_dir = settings.get_backup_copy_dir()
        if not copy_dir:
            # Ask for path
            _bk_state[chat_id] = {'awaiting': 'bk_copy_dir_now'}
            await query.edit_message_text(
                '📁 <b>Copy MDB to Directory</b>\n\n'
                'No copy directory is configured yet.\n\n'
                'Reply with the full destination path.\n'
                'Example: <code>/mnt/backups/mdb</code>\n\n'
                'This will also save the path for future copies and scheduled backups.',
                parse_mode=ParseMode.HTML
            )
            return
        # Copy immediately
        local_path, _, accessible = _get_mdb_size_info()
        if not accessible:
            await query.edit_message_text('❌ MDB is not accessible. Check /mdbinfo.')
            return
        await query.edit_message_text('⏳ Copying MDB…')
        stamp    = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"mdb_backup_{stamp}.mdb"
        try:
            os.makedirs(copy_dir, exist_ok=True)
            dest = os.path.join(copy_dir, filename)
            shutil.copy2(local_path, dest)
            _audit('dbbackup.copy', update, f'dest={dest}')
            await query.edit_message_text(
                f'✅ MDB copied to:\n<code>{dest}</code>', parse_mode=ParseMode.HTML
            )
        except OSError as e:
            _audit('dbbackup.copy.error', update, str(e))
            await query.edit_message_text(f'❌ Copy failed: {e}')
        return

    # ── Schedule settings sub-panel ───────────────────────────────────────────

    if action == 'sched':
        text, kb = _fmt_backup_sched_panel()
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if action == 'sback':
        text, kb = _fmt_backup_sched_panel()
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if action == 'sdone':
        _bk_state.pop(chat_id, None)
        await query.edit_message_text('✅ Backup schedule settings saved.')
        return

    if action == 'stoggle':
        settings.set_backup_enabled(not settings.get_backup_enabled())
        _audit('dbbackup.sched.toggle', update,
               f'enabled={settings.get_backup_enabled()}')
        text, kb = _fmt_backup_sched_panel()
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if action == 'sfreq':
        await query.edit_message_text(
            '🔄 <b>Select Backup Frequency</b>',
            parse_mode=ParseMode.HTML,
            reply_markup=_backup_freq_menu_kb(),
        )
        return

    if action.startswith('sfr:'):
        val = action[4:].strip().lower()
        if val in _BACKUP_FREQ_LABELS:
            settings.set_backup_schedule(val)
            _audit('dbbackup.sched.freq', update, f'freq={val}')
        text, kb = _fmt_backup_sched_panel()
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if action == 'stime':
        _bk_state[chat_id] = {'awaiting': 'bk_time'}
        current = settings.backup_time_label()
        await query.edit_message_text(
            '🕐 <b>Set Backup Schedule Time</b>\n\n'
            f'Current: <b>{current}</b>\n\n'
            'Reply with the time in <b>HH:MM</b> format (24 h, zero-padded).\n'
            'Example: <code>07:00</code>',
            parse_mode=ParseMode.HTML,
        )
        return

    if action == 'sdays':
        await query.edit_message_text(
            '📅 <b>Select Backup Days</b>\n(tap to toggle; used for weekly schedule)',
            parse_mode=ParseMode.HTML,
            reply_markup=_backup_days_menu_kb(),
        )
        return

    if action.startswith('sday:'):
        try:
            day_num = int(action[5:])
        except ValueError:
            return
        current = settings.get_backup_days()
        sel = {int(d.strip()) for d in current.split(',') if d.strip().isdigit()}
        if day_num in sel:
            sel.discard(day_num)
            if not sel:
                # Prevent completely empty selection — keep current day toggled
                sel.add(day_num)
        else:
            sel.add(day_num)
        settings.set_backup_days(','.join(str(d) for d in sorted(sel)))
        _audit('dbbackup.sched.days', update, f'days={settings.get_backup_days()}')
        await query.edit_message_reply_markup(reply_markup=_backup_days_menu_kb())
        return

    if action == 'smeth':
        await query.edit_message_text(
            '📬 <b>Select Backup Delivery Method</b>',
            parse_mode=ParseMode.HTML,
            reply_markup=_backup_method_menu_kb(),
        )
        return

    if action.startswith('smeth:'):
        val = action[6:].strip().lower()
        if val in _BACKUP_METHOD_LABELS:
            settings.set_backup_method(val)
            _audit('dbbackup.sched.method', update, f'method={val}')
        text, kb = _fmt_backup_sched_panel()
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if action == 'scopy':
        _bk_state[chat_id] = {'awaiting': 'bk_copy_dir'}
        current_dir = settings.get_backup_copy_dir() or '—'
        await query.edit_message_text(
            '📁 <b>Set Backup Copy Directory</b>\n\n'
            f'Current: <code>{current_dir}</code>\n\n'
            'Reply with the full destination path.\n'
            'Example: <code>/mnt/backups/mdb</code>\n'
            'Send <code>none</code> to clear.',
            parse_mode=ParseMode.HTML,
        )
        return

    # ── Backup mail settings sub-panel ────────────────────────────────────────

    if action == 'bkmail':
        text, kb = _fmt_backup_mail_panel()
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if action == 'mback':
        text, kb = _fmt_backup_sched_panel()
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if action == 'msender':
        _bk_state[chat_id] = {'awaiting': 'bk_sender'}
        raw     = settings.get_backup_sender_email_raw()
        current = raw or f'(smtp fallback: {settings.get_smtp_sender_email() or "—"})'
        await query.edit_message_text(
            '📤 <b>Set Backup Sender Email</b>\n\n'
            f'Current: <code>{current}</code>\n\n'
            'Reply with your Gmail address.\n'
            'Send <code>none</code> to clear (use smtp fallback).',
            parse_mode=ParseMode.HTML,
        )
        return

    if action == 'mname':
        _bk_state[chat_id] = {'awaiting': 'bk_name'}
        raw     = settings.get_backup_sender_name_raw()
        current = raw or f'(smtp fallback: {settings.get_smtp_sender_name() or "—"})'
        await query.edit_message_text(
            '👤 <b>Set Backup Sender Name</b>\n\n'
            f'Current: <b>{current}</b>\n\n'
            'Reply with the display name for the From: header.\n'
            'Send <code>none</code> to clear (use smtp fallback).',
            parse_mode=ParseMode.HTML,
        )
        return

    if action == 'mpass':
        _bk_state[chat_id] = {'awaiting': 'bk_pass'}
        raw     = settings.get_backup_app_password_raw()
        set_str = '✅ Set (backup-specific)' if raw else '(smtp fallback)'
        await query.edit_message_text(
            '🔑 <b>Set Backup App Password</b>\n\n'
            f'Status: <b>{set_str}</b>\n\n'
            'Reply with your Gmail App Password (16 chars, no spaces).\n'
            'Get one at: Google Account → Security → App Passwords.\n'
            'Send <code>none</code> to clear (use smtp fallback).\n\n'
            '⚠️ Stored in config.ini — do not share that file.',
            parse_mode=ParseMode.HTML,
        )
        return

    if action == 'mrecip':
        await query.edit_message_text(
            _backup_recipients_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=_backup_recipients_menu_kb(),
        )
        return

    if action == 'madd':
        _bk_state[chat_id] = {'awaiting': 'bk_add_recip'}
        await query.edit_message_text(
            '➕ <b>Add Backup Recipient</b>\n\n'
            'Reply with the email address to add.\n'
            'Example: <code>itsupport@school.ae</code>',
            parse_mode=ParseMode.HTML,
        )
        return

    if action.startswith('mrm:'):
        try:
            idx = int(action[4:])
        except (ValueError, IndexError):
            return
        recipients = settings.get_backup_recipients_raw()
        if 0 <= idx < len(recipients):
            removed = recipients.pop(idx)
            settings.set_backup_recipients(recipients)
            _audit('dbbackup.mail.recip.rm', update, f'removed={removed}')
            await query.edit_message_text(
                _backup_recipients_text() + f'\n\n✅ Removed: <code>{removed}</code>',
                parse_mode=ParseMode.HTML,
                reply_markup=_backup_recipients_menu_kb(),
            )
        return



async def cmd_importcsv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Read-only CSV preview/validation workflow."""
    if not _allowed(update):
        return await _deny(update)
    chat_id = str(update.effective_chat.id)
    _importcsv_state[chat_id] = True
    await update.message.reply_text(
        "📄 <b>CSV Preview Mode (Read-Only)</b>\n\n"
        "Upload a CSV file now. I will only validate structure and show a preview.\n"
        "No rows are imported and MDB is never modified.",
        parse_mode=ParseMode.HTML
    )

async def cmd_autonmap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Suggest UID→badge matches without persisting them."""
    if not _allowed(update):
        return await _deny(update)
    await update.message.reply_text('⏳ Building read-only auto-map suggestions...')
    try:
        emps = mdb_reader.get_employees(active_only=False)
        known = {e['badge'] for e in emps}
        unknown = zk_devices.get_unknown_users(known)
        if not unknown:
            await update.message.reply_text('✅ No unknown device users found.')
            return
        by_name = {}
        for e in emps:
            key = ''.join(ch for ch in e['name'].lower() if ch.isalnum())
            if key:
                by_name.setdefault(key, []).append(e)

        lines = [
            "🧩 <b>Auto-map Suggestions (Read-Only)</b>",
            "Suggestions are not saved to MDB or devices.",
            "",
        ]
        shown = 0
        for u in unknown[:30]:
            raw_name = (u.get('name_on_device') or '').strip()
            nkey = ''.join(ch for ch in raw_name.lower() if ch.isalnum())
            cands = by_name.get(nkey, []) if nkey else []
            line = (f"• UID:{u['uid']} ID:{u['user_id']} "
                    f"({u['device_name']} {u['device_ip']})")
            if cands:
                top = cands[0]
                line += f" → suggest [{top['badge']}] {top['name']} ({top['dept']})"
            else:
                line += " → no strong match"
            lines.append(line)
            shown += 1
        if len(unknown) > shown:
            lines.append(f"\n…and {len(unknown) - shown} more unknown users.")
        for chunk in _split('\n'.join(lines)):
            await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def cmd_shifts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Read-only view of shift-related settings."""
    if not _allowed(update):
        return await _deny(update)
    shift_start = _cfg.get('attendance', 'shift_start', fallback='07:30').strip()
    await update.message.reply_text(
        "🕐 <b>Shift Settings (Read-Only)</b>\n\n"
        f"Shift start (for late/early): <b>{shift_start}</b>\n"
        "Used by /late, /early, and /employeereport.\n"
        "No configuration changes are made from this command.",
        parse_mode=ParseMode.HTML
    )

async def cmd_workdays(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Read-only view of workday configuration."""
    if not _allowed(update):
        return await _deny(update)
    days_raw = settings.get_daily_days()
    day_nums = [int(d.strip()) for d in days_raw.split(',') if d.strip().isdigit()]
    labels = ', '.join(_DAY_NAMES.get(d, str(d)) for d in day_nums) or '—'
    await update.message.reply_text(
        "📅 <b>Workdays (Read-Only)</b>\n\n"
        f"Configured daily report days: <b>{labels}</b>\n"
        "Attendance weekend in reports is fixed as: <b>Fri, Sat</b>.\n"
        "No MDB or settings writes are performed.",
        parse_mode=ParseMode.HTML
    )

async def handle_document_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Process CSV uploads for /importcsv preview mode only."""
    if not _allowed(update):
        return
    chat_id = str(update.effective_chat.id)
    if not _importcsv_state.get(chat_id):
        return
    doc = update.message.document
    filename = (doc.file_name or '').strip()
    if not filename.lower().endswith('.csv'):
        await update.message.reply_text('❌ Please upload a .csv file (preview mode is still active).')
        return
    used_latin1_fallback = False
    try:
        tg_file = await doc.get_file()
        data = await tg_file.download_as_bytearray()
    except Exception as e:
        await update.message.reply_text(f'❌ Failed to download CSV: {e}')
        return
    try:
        text = bytes(data).decode('utf-8-sig')
    except UnicodeDecodeError:
        logger.info(f'CSV {filename} not UTF-8, falling back to latin-1 decode.')
        text = bytes(data).decode('latin-1')
        used_latin1_fallback = True

    try:
        df = pd.read_csv(StringIO(text), dtype=str)
        columns = [str(c).strip() for c in df.columns]
        preview = df.head(10).fillna('').astype(str)
        cols_preview = ', '.join(columns[:20]) if columns else '—'
        lines = [
            "✅ <b>CSV Validation Complete (Read-Only)</b>",
            f"File: <code>{html.escape(filename)}</code>",
            f"Rows: {len(df)}",
            f"Columns ({len(columns)}): {html.escape(cols_preview)}",
            "",
            "No data was imported into MDB.",
        ]
        if preview.empty:
            lines.append("\nPreview: file has headers but no data rows.")
        else:
            preview_display = preview.iloc[:, :12].copy()
            for col in preview_display.columns:
                preview_display[col] = preview_display[col].astype(str).str.slice(0, 40)
            preview_csv_lines = html.escape(preview_display.to_csv(index=False).strip()).splitlines()
            kept_lines, total_len = [], 0
            for ln in preview_csv_lines:
                newline_overhead = 1 if kept_lines else 0
                add_len = len(ln) + newline_overhead
                if total_len + add_len > CSV_PREVIEW_MAX_CHARS:
                    break
                kept_lines.append(ln)
                total_len += add_len
            preview_csv_lines = kept_lines
            preview_csv = '\n'.join(preview_csv_lines)
            if used_latin1_fallback:
                lines.append("\n⚠️ File was not UTF-8; decoded using latin-1 fallback.")
            lines.append("\n<b>Preview (first 10 rows):</b>")
            lines.append("<code>" + preview_csv + "</code>")
        for chunk in _split('\n'.join(lines)):
            await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ CSV parse error: {e}')
    finally:
        _importcsv_state.pop(chat_id, None)

# ─── Utilities ────────────────────────────────────────────────────────────────

def _split(text: str, limit: int = 4000) -> list:
    """Split long messages for Telegram's 4096 char limit."""
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ''
    for line in text.split('\n'):
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = ''
        cur += line + '\n'
    if cur:
        chunks.append(cur)
    return chunks

async def unknown_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    await update.message.reply_text('❓ Unknown command. Send /help for list.')

# Alias so /unknown command handler has a distinct name from the fallback above
async def cmd_unknown_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    return await cmd_unknown(update, ctx)

# ─── Main ─────────────────────────────────────────────────────────────────────

async def post_init(app):
    """Start scheduler after bot initializes."""
    bot = app.bot
    asyncio.create_task(notifier.run_scheduler(bot))
    logger.info('Scheduler started.')

def main():
    if not BOT_TOKEN or BOT_TOKEN == 'YOUR_BOT_TOKEN_HERE':
        logger.error('Bot token not set in config.ini')
        sys.exit(1)
    if not CHAT_ID or CHAT_ID == 'YOUR_CHAT_ID_HERE':
        logger.error('chat_id not set in config.ini')
        sys.exit(1)

    logger.info('Starting ZKTeco Attendance Bot...')

    app = (Application.builder()
           .token(BOT_TOKEN)
           .post_init(post_init)
           .build())

    # Register handlers
    handlers = [
        ('start',       cmd_start),
        ('help',        cmd_help),
        # Attendance
        ('today',       cmd_today),
        ('absent',      cmd_absent),
        ('present',     cmd_present),
        ('late',        cmd_late),
        ('early',       cmd_early),
        ('whoisin',     cmd_whoisin),
        ('feed',        cmd_feed),
        ('latest',      cmd_latest),
        ('week',        cmd_week),
        ('month',       cmd_month),
        ('topabsent',   cmd_topabsent),
        ('dept',        cmd_dept),
        ('history',     cmd_history),
        ('syncrange',   cmd_syncrange),
        ('trend',       cmd_trend),
        ('report',      cmd_report),
        # Employee
        ('search',      cmd_search),
        ('punches',     cmd_punches),
        ('employeereport', cmd_employeereport),
        ('calendar',    cmd_calendar),
        # Devices
        ('device',      cmd_device),
        ('devices',     cmd_devices),
        ('clocksync',   cmd_clocksync),
        ('reboot',      cmd_reboot),
        ('usersync',    cmd_usersync),
        ('adduser',     cmd_adduser),
        ('unknown',     cmd_unknown_users),
        # Settings
        ('livepunches', cmd_livepunches),
        ('editreport',  cmd_editreport),
        ('editdaily',   cmd_editdaily),
        ('editemail',   cmd_editemail),
        ('mail',        cmd_mail),
        # Admin & Security
        ('admin',       cmd_admin),
        ('shell',       cmd_shell),
        ('su',          cmd_su),
        ('sql',         cmd_sql),
        ('auditlog',    cmd_auditlog),
        ('presence',    cmd_presence),
        ('status',      cmd_presence),
        ('exit',        cmd_exit),
        # DB
        ('stats',       cmd_stats),
        ('mdbinfo',     cmd_mdbinfo),
        ('setmdb',      cmd_setmdb),
        ('tables',      cmd_tables),
        ('download',    cmd_download),
        ('dbbackup',    cmd_dbbackup),
        ('importcsv',   cmd_importcsv),
        ('autonmap',    cmd_autonmap),
        ('shifts',      cmd_shifts),
        ('workdays',    cmd_workdays),
    ]

    for cmd, handler in handlers:
        app.add_handler(CommandHandler(cmd, handler))

    # Callback query handlers — route by prefix
    app.add_handler(CallbackQueryHandler(callback_edit,
                                         pattern=r'^(er:|ed:|ee:)'))
    app.add_handler(CallbackQueryHandler(callback_admin, pattern=r'^adm:'))
    app.add_handler(CallbackQueryHandler(callback_device, pattern=r'^dev:'))
    app.add_handler(CallbackQueryHandler(callback_dbbackup, pattern=r'^bk:'))
    app.add_handler(CallbackQueryHandler(callback_calendar))

    # Text input handler (for edit panel awaiting states)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,
                                   handle_text_input))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document_input))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    logger.info('Bot running. Press Ctrl+C to stop.')
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
