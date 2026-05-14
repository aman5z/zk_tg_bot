"""
bot.py — ZKTeco Attendance Telegram Bot
Read-only from Middle East Attendance MDB + ZK device control.
No dashboard, no Flask, no SQLite. Telegram only.

Run: python bot.py
"""

import asyncio
import calendar as _cal
import configparser
import logging
import os
import sys
from datetime import date, datetime, timedelta
from io import BytesIO

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

# ─── Auth ─────────────────────────────────────────────────────────────────────

def _allowed(update: Update) -> bool:
    uid  = str(update.effective_user.id)
    cid  = str(update.effective_chat.id)
    return cid == CHAT_ID or uid == CHAT_ID or uid in EXTRA_USERS

async def _deny(update: Update):
    await update.message.reply_text('⛔ Unauthorized.')

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
        line = f"{icon} <b>{s['name']}</b> ({s['ip']})"
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
        "/whoisin — currently inside building\n"
        "/feed — last 20 punches\n"
        "/latest — device status + last 2 MDB punches per device\n"
        "/week — this week summary\n"
        "/month — monthly dept summary\n"
        "/topabsent — most absent this month\n"
        "/history &lt;DD/MM/YYYY&gt; &lt;DD/MM/YYYY&gt; — range report\n"
        "/report — send absent report (XLSX/PNG/PDF)\n\n"
        "<b>Employee</b>\n"
        "/search &lt;name or badge&gt; — find employee\n"
        "/punches &lt;badge&gt; — today's punches\n"
        "/calendar &lt;badge&gt; — interactive date/range picker\n"
        "/calendar &lt;badge&gt; YYYY-MM — static monthly calendar\n\n"
        "<b>Devices</b>\n"
        "/devices — all device status\n"
        "/clocksync — sync all device clocks\n"
        "/reboot &lt;ip or name&gt; | all — reboot a device or all\n"
        "/usersync — sync users across devices\n"
        "/adduser &lt;badge&gt; &lt;name&gt; — add user to all devices\n"
        "/unknown — users on devices not in MDB\n\n"
        "<b>Settings</b>\n"
        "/livepunches — toggle live punch notifications on/off\n"
        "/editreport — configure on-demand /report settings\n"
        "/editdaily — configure scheduled daily report settings\n\n"
        "<b>Database</b>\n"
        "/stats — MDB stats\n"
        "/mdbinfo — MDB path + file info\n"
        "/setmdb &lt;path&gt; — update MDB path\n"
        "/tables — list MDB tables (diagnostics)\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

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
# Telegram callback_data has a 64-byte limit.
# Prefix like 'er:dept:' = 8 chars, leaving 56 for dept name.
# We cap at 50 chars to be safe.
_MAX_DEPT_CALLBACK_LEN = 50


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


def _days_menu_kb(current: str) -> InlineKeyboardMarkup:
    selected = {int(d.strip()) for d in current.split(',')
                if d.strip().isdigit()}
    rows = []
    btns = []
    for day_num, day_name in _DAY_NAMES.items():
        checked = day_num in selected
        btns.append(InlineKeyboardButton(
            f"{'✅' if checked else '⬜'} {day_name}",
            callback_data=f'ed:day:{day_num}',
        ))
    # 3 per row
    for i in range(0, len(btns), 3):
        rows.append(btns[i:i + 3])
    rows.append([InlineKeyboardButton('← Back', callback_data='ed:back')])
    return InlineKeyboardMarkup(rows)


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
            settings.set_daily_days(','.join(str(d) for d in sorted(sel)) if sel else '0,1,2,3,6')
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


# ── Text input handler for awaiting states ────────────────────────────────────

async def handle_text_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Capture plain text input when an edit panel is awaiting a value."""
    if not _allowed(update):
        return
    chat_id = str(update.effective_chat.id)
    st = _edit_state.get(chat_id)
    if not st or not st.get('awaiting'):
        return  # nothing awaiting — ignore

    text = (update.message.text or '').strip()
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
        return

async def _edit_or_followup(query, update: Update, text: str):
    """Edit the callback message with text; if too long, send follow-up chunks."""
    chunks = _split(text)
    await query.edit_message_text(chunks[0], parse_mode=ParseMode.HTML)
    for chunk in chunks[1:]:
        await update.effective_chat.send_message(chunk, parse_mode=ParseMode.HTML)

# ── Device commands ───────────────────────────────────────────────────────────

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
        ('whoisin',     cmd_whoisin),
        ('feed',        cmd_feed),
        ('latest',      cmd_latest),
        ('week',        cmd_week),
        ('month',       cmd_month),
        ('topabsent',   cmd_topabsent),
        ('history',     cmd_history),
        ('report',      cmd_report),
        # Employee
        ('search',      cmd_search),
        ('punches',     cmd_punches),
        ('calendar',    cmd_calendar),
        # Devices
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
        # DB
        ('stats',       cmd_stats),
        ('mdbinfo',     cmd_mdbinfo),
        ('setmdb',      cmd_setmdb),
        ('tables',      cmd_tables),
    ]

    for cmd, handler in handlers:
        app.add_handler(CommandHandler(cmd, handler))

    # Callback query handlers — route by prefix
    app.add_handler(CallbackQueryHandler(callback_edit,
                                         pattern=r'^(er:|ed:)'))
    app.add_handler(CallbackQueryHandler(callback_calendar))

    # Text input handler (for edit panel awaiting states)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,
                                   handle_text_input))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    logger.info('Bot running. Press Ctrl+C to stop.')
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
