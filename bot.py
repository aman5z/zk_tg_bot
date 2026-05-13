"""
bot.py — ZKTeco Attendance Telegram Bot
Read-only from Middle East Attendance MDB + ZK device control.
No dashboard, no Flask, no SQLite. Telegram only.

Run: python bot.py
"""

import asyncio
import configparser
import logging
import os
import sys
from datetime import date, datetime, timedelta
from io import BytesIO

import pandas as pd
from telegram import Update, Bot
from telegram.ext import (Application, CommandHandler, MessageHandler,
                           filters, ContextTypes)
from telegram.constants import ParseMode

import mdb_reader
import zk_devices
import notifier

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
        "/week — this week summary\n"
        "/month — monthly dept summary\n"
        "/topabsent — most absent this month\n"
        "/history &lt;DD/MM/YYYY&gt; &lt;DD/MM/YYYY&gt; — range report\n"
        "/report — send absent XLSX\n\n"
        "<b>Employee</b>\n"
        "/search &lt;name or badge&gt; — find employee\n"
        "/punches &lt;badge&gt; — today's punches\n"
        "/calendar &lt;badge&gt; [YYYY-MM] — monthly calendar\n\n"
        "<b>Devices</b>\n"
        "/devices — all device status\n"
        "/clocksync — sync all device clocks\n"
        "/reboot &lt;ip or name&gt; | all — reboot a device or all\n"
        "/usersync — sync users across devices\n"
        "/adduser &lt;badge&gt; &lt;name&gt; — add user to all devices\n"
        "/unknown — users on devices not in MDB\n\n"
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
    """Send today's absent list as XLSX."""
    if not _allowed(update):
        return await _deny(update)
    await update.message.reply_text('⏳ Building report...')
    try:
        bot = ctx.bot
        await notifier.send_daily_report(bot)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

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
    """Usage: /calendar <badge> [YYYY-MM]"""
    if not _allowed(update):
        return await _deny(update)
    if not ctx.args:
        await update.message.reply_text(
            'Usage: /calendar &lt;badge&gt; [YYYY-MM]\n'
            'Example: /calendar 1024 2026-05', parse_mode=ParseMode.HTML)
        return
    badge = ctx.args[0].strip()
    month_str = ctx.args[1] if len(ctx.args) > 1 else date.today().strftime('%Y-%m')
    try:
        year, month = map(int, month_str.split('-'))
    except ValueError:
        await update.message.reply_text('❌ Month format: YYYY-MM')
        return
    await update.message.reply_text('⏳ Building calendar...')
    try:
        cal = mdb_reader.get_employee_calendar(badge, year, month)
        text = _fmt_calendar(cal)
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

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
        ('start',     cmd_start),
        ('help',      cmd_help),
        # Attendance
        ('today',     cmd_today),
        ('absent',    cmd_absent),
        ('present',   cmd_present),
        ('late',      cmd_late),
        ('whoisin',   cmd_whoisin),
        ('feed',      cmd_feed),
        ('week',      cmd_week),
        ('month',     cmd_month),
        ('topabsent', cmd_topabsent),
        ('history',   cmd_history),
        ('report',    cmd_report),
        # Employee
        ('search',    cmd_search),
        ('punches',   cmd_punches),
        ('calendar',  cmd_calendar),
        # Devices
        ('devices',   cmd_devices),
        ('clocksync', cmd_clocksync),
        ('reboot',    cmd_reboot),
        ('usersync',  cmd_usersync),
        ('adduser',   cmd_adduser),
        ('unknown',   cmd_unknown_users),
        # DB
        ('stats',     cmd_stats),
        ('mdbinfo',   cmd_mdbinfo),
        ('setmdb',    cmd_setmdb),
        ('tables',    cmd_tables),
    ]

    for cmd, handler in handlers:
        app.add_handler(CommandHandler(cmd, handler))

    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    logger.info('Bot running. Press Ctrl+C to stop.')
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
