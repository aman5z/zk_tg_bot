"""
notifier.py
Scheduled Telegram notifications:
- Daily absent report at configured time
- Device status changes
- Live punch notifications (optional, toggle with /livepunches)
"""

import asyncio
import logging
import time
from datetime import datetime, date
from io import BytesIO
from typing import Optional

from telegram import Bot
from telegram.error import TelegramError

import mdb_reader
import zk_devices
import settings
import report_builder

logger = logging.getLogger(__name__)


# ─── Send helpers ─────────────────────────────────────────────────────────────

def _chat_id() -> str:
    return settings.get_chat_id()


async def _send(bot: Bot, text: str, parse_mode: str = 'HTML'):
    try:
        await bot.send_message(chat_id=_chat_id(), text=text,
                               parse_mode=parse_mode)
    except TelegramError as e:
        logger.error(f"Telegram send error: {e}")


async def _send_doc(bot: Bot, data: BytesIO, filename: str, caption: str = ''):
    try:
        data.seek(0)
        await bot.send_document(chat_id=_chat_id(), document=data,
                                filename=filename, caption=caption)
    except TelegramError as e:
        logger.error(f"Telegram send_document error: {e}")


async def _send_photo(bot: Bot, data: BytesIO, caption: str = ''):
    try:
        data.seek(0)
        await bot.send_photo(chat_id=_chat_id(), photo=data, caption=caption)
    except TelegramError as e:
        logger.error(f"Telegram send_photo error: {e}")


# ─── Daily absent report ──────────────────────────────────────────────────────

async def send_daily_report(
    bot: Bot,
    departments: str = None,
    formats: str = None,
    template: str = None,
    extra_exclude_badges: set = None,
):
    """
    Build absent list and send in configured format(s) to Telegram.
    When arguments are None, values are read from settings (config.ini).
    """
    if departments is None:
        departments = settings.get_daily_departments()
    if formats is None:
        formats = settings.get_daily_formats()
    if template is None:
        template = settings.get_daily_template()
    if extra_exclude_badges is None:
        raw = settings.get_daily_exclude_badges()
        extra_exclude_badges = {b.strip() for b in raw.split(',') if b.strip()}

    try:
        summary = mdb_reader.get_today_summary()
        absent  = summary['absent']
        today   = date.today()

        dept_lines = []
        for dept, s in sorted(summary['dept_stats'].items()):
            total = s['present'] + s['absent']
            pct   = round(s['present'] / total * 100) if total else 0
            dept_lines.append(
                f"  {'🟢' if pct >= 90 else '🟡' if pct >= 70 else '🔴'} "
                f"{dept}: {s['present']}/{total} ({pct}%)"
            )

        if not absent:
            await _send(
                bot,
                f"✅ <b>Daily Report — {summary['date']}</b>\n\nNo absences today! All staff present."
            )
            return

        msg = (
            f"📋 <b>Daily Absent Report</b>\n"
            f"📅 {summary['date']}\n\n"
            f"👥 Total: {summary['total']} | "
            f"✅ Present: {summary['present_count']} | "
            f"❌ Absent: {summary['absent_count']}\n\n"
            f"<b>By Department:</b>\n" + '\n'.join(dept_lines)
        )
        await _send(bot, msg)

        # Build and send report files
        files = report_builder.build_absent_report(
            absent=absent,
            report_date=today,
            departments=departments,
            formats=formats,
            template=template,
            extra_exclude_badges=extra_exclude_badges,
        )

        caption = f'Absent list — {today.strftime("%d/%m/%Y")}'
        for fmt_key, (buf, filename) in files.items():
            if fmt_key == 'png':
                await _send_photo(bot, buf, caption=caption)
            else:
                await _send_doc(bot, buf, filename=filename, caption=caption)

    except Exception as e:
        logger.error(f"Daily report error: {e}")
        await _send(bot, f"⚠️ Daily report failed: {e}")


# ─── Device status monitor ────────────────────────────────────────────────────

_last_device_status: dict = {}   # ip → online bool


async def check_device_status_changes(bot: Bot):
    """
    Send alert if any device changes online/offline state.
    This runs unconditionally — device monitoring is always enabled.
    """
    try:
        statuses = zk_devices.get_device_status()
        for s in statuses:
            ip          = s['ip']
            name        = s['name']
            now_online  = s['online']
            prev_online = _last_device_status.get(ip)

            if prev_online is None:
                _last_device_status[ip] = now_online
                continue

            if prev_online != now_online:
                icon  = '🟢' if now_online else '🔴'
                state = 'ONLINE' if now_online else 'OFFLINE'
                await _send(
                    bot,
                    f"{icon} <b>Device {state}</b>\n"
                    f"📍 {name} ({ip})\n"
                    f"🕐 {datetime.now().strftime('%H:%M:%S')}"
                )
                _last_device_status[ip] = now_online
    except Exception as e:
        logger.error(f"Device status check error: {e}")


# ─── Live punch notifications ─────────────────────────────────────────────────

_last_live_punch_ts: Optional[datetime] = None   # timestamp of the last punch we notified about


async def check_live_punches(bot: Bot):
    """Send a notification for every new punch since the last check."""
    global _last_live_punch_ts
    if not settings.get_live_punches():
        return
    try:
        now   = datetime.now()
        since = _last_live_punch_ts or datetime(now.year, now.month, now.day, 0, 0, 0)
        new_punches = mdb_reader.get_live_punches_since(since)
        for p in new_punches:
            dev = p.get('device') or '?'
            await _send(
                bot,
                #f"👆 <b>Live Punch</b>\n"
                f"🕐 {p['time']}  👤 {p['name']}\n"
                f"🏷 Badge: {p['badge']}  🏢 {p['dept']}\n"
                f"📡 Device: <code>{dev}</code>"
            )
            _last_live_punch_ts = p['timestamp']
    except Exception as e:
        logger.error(f"Live punch check error: {e}")


# ─── Scheduler loop ───────────────────────────────────────────────────────────

async def run_scheduler(bot: Bot):
    """
    Async scheduler loop.
    - Device online/offline alerts: every 5 minutes, always-on (not configurable).
    - Live punches: every minute, optional — toggle with /livepunches.
    - Daily report: once per day at configured time on configured days.
    """
    report_sent_today = None
    device_check_interval = 300   # 5 min
    last_device_check     = 0.0

    logger.info(
        f"Scheduler started. Daily report at "
        f"{settings.get_daily_hour():02d}:{settings.get_daily_minute():02d}"
    )

    while True:
        try:
            now = datetime.now()
            ts  = time.time()

            # Device status — every 5 min
            if ts - last_device_check >= device_check_interval:
                await check_device_status_changes(bot)
                last_device_check = ts

            # Live punches — every cycle
            await check_live_punches(bot)

            # Daily report — once per day at configured time
            report_h = settings.get_daily_hour()
            report_m = settings.get_daily_minute()
            today    = now.date()

            daily_days = {int(d.strip()) for d in settings.get_daily_days().split(',')
                          if d.strip().isdigit()}
            if not daily_days:
                logger.warning("daily_days is empty or invalid; using Mon-Thu+Sun fallback")
                daily_days = {0, 1, 2, 3, 6}
            is_report_day = today.weekday() in daily_days

            if (is_report_day
                    and now.hour == report_h
                    and now.minute == report_m
                    and report_sent_today != today):
                await send_daily_report(bot)
                report_sent_today = today

        except Exception as e:
            logger.error(f"Scheduler loop error: {e}")

        await asyncio.sleep(60)
