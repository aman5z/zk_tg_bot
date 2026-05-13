"""
notifier.py
Scheduled Telegram notifications:
- Daily absent report at configured time
- Device status changes
- (Punch notifications optional — can be noisy)
"""

import asyncio
import configparser
import logging
import os
import time
from datetime import datetime, date
from io import BytesIO

import pandas as pd
from telegram import Bot
from telegram.error import TelegramError

import mdb_reader
import zk_devices

logger = logging.getLogger(__name__)

_cfg = configparser.ConfigParser()
_cfg.read(os.path.join(os.path.dirname(__file__), 'config.ini'))

def _chat_id() -> str:
    return _cfg.get('telegram', 'chat_id', fallback='').strip()

def _bot_token() -> str:
    return _cfg.get('telegram', 'bot_token', fallback='').strip()

def _notify_device_status() -> bool:
    return _cfg.getboolean('notifications', 'notify_device_status', fallback=True)

def _notify_punches() -> bool:
    return _cfg.getboolean('notifications', 'notify_punches', fallback=False)

def _report_time() -> tuple:
    h = _cfg.getint('notifications', 'daily_report_hour', fallback=8)
    m = _cfg.getint('notifications', 'daily_report_minute', fallback=10)
    return h, m

# ─── Send helpers ─────────────────────────────────────────────────────────────

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

# ─── Daily absent report ──────────────────────────────────────────────────────

async def send_daily_report(bot: Bot):
    """Build absent list and send as XLSX to Telegram."""
    try:
        summary = mdb_reader.get_today_summary()
        absent = summary['absent']
        today_str = date.today().strftime('%d_%m_%Y')

        if not absent:
            await _send(bot, f"✅ <b>Daily Report — {summary['date']}</b>\n\nNo absences today! All staff present.")
            return

        # Build XLSX in memory
        rows = [{'Badge': e['badge'], 'Name': e['name'],
                 'Department': e['dept']} for e in absent]
        df = pd.DataFrame(rows)
        buf = BytesIO()
        with pd.ExcelWriter(buf, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Absent')
        buf.seek(0)

        # Summary message
        dept_lines = []
        for dept, s in sorted(summary['dept_stats'].items()):
            total = s['present'] + s['absent']
            pct   = round(s['present'] / total * 100) if total else 0
            dept_lines.append(
                f"  {'🟢' if pct >= 90 else '🟡' if pct >= 70 else '🔴'} "
                f"{dept}: {s['present']}/{total} ({pct}%)"
            )

        msg = (
            f"📋 <b>Daily Absent Report</b>\n"
            f"📅 {summary['date']}\n\n"
            f"👥 Total: {summary['total']} | "
            f"✅ Present: {summary['present_count']} | "
            f"❌ Absent: {summary['absent_count']}\n\n"
            f"<b>By Department:</b>\n" + '\n'.join(dept_lines)
        )
        await _send(bot, msg)
        await _send_doc(bot, buf, f'Absent_{today_str}.xlsx',
                        f'Absent list — {date.today().strftime("%d/%m/%Y")}')

    except Exception as e:
        logger.error(f"Daily report error: {e}")
        await _send(bot, f"⚠️ Daily report failed: {e}")

# ─── Device status monitor ────────────────────────────────────────────────────

_last_device_status: dict = {}   # ip → online bool

async def check_device_status_changes(bot: Bot):
    """Send alert if any device changes online/offline state."""
    if not _notify_device_status():
        return
    try:
        statuses = zk_devices.get_device_status()
        for s in statuses:
            ip   = s['ip']
            name = s['name']
            now_online = s['online']
            prev_online = _last_device_status.get(ip)

            if prev_online is None:
                # First run — just record, don't alert
                _last_device_status[ip] = now_online
                continue

            if prev_online != now_online:
                icon = '🟢' if now_online else '🔴'
                state = 'ONLINE' if now_online else 'OFFLINE'
                await _send(bot,
                    f"{icon} <b>Device {state}</b>\n"
                    f"📍 {name} ({ip})\n"
                    f"🕐 {datetime.now().strftime('%H:%M:%S')}")
                _last_device_status[ip] = now_online
    except Exception as e:
        logger.error(f"Device status check error: {e}")

# ─── Scheduler loop ───────────────────────────────────────────────────────────

async def run_scheduler(bot: Bot):
    """
    Async scheduler loop.
    - Device status: every 5 minutes
    - Daily report: at configured time (default 08:10)
    """
    report_h, report_m = _report_time()
    report_sent_today = None
    device_check_interval = 300  # 5 min
    last_device_check = 0.0

    logger.info(f"Scheduler started. Daily report at {report_h:02d}:{report_m:02d}")

    while True:
        try:
            now = datetime.now()
            ts = time.time()

            # Device status check every 5 min
            if ts - last_device_check >= device_check_interval:
                await check_device_status_changes(bot)
                last_device_check = ts

            # Daily report — once per day at configured time
            today = now.date()
            if (now.hour == report_h and now.minute == report_m
                    and report_sent_today != today):
                await send_daily_report(bot)
                report_sent_today = today

        except Exception as e:
            logger.error(f"Scheduler loop error: {e}")

        await asyncio.sleep(60)  # check every minute
