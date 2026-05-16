"""
notifier.py
Scheduled Telegram notifications:
- Daily absent report at configured time
- Device status changes
- Live punch notifications (optional, toggle with /livepunches)
- Scheduled MDB backup (optional, toggle with /dbbackup → Schedule Settings)
"""

import asyncio
import logging
import os
import shutil
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
import email_sender

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
        generated_at = datetime.now().strftime('%d-%m-%Y %I:%M%p')

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
                f"✅ <b>Daily Report — {summary['date']}</b>\n\n"
                f"No absences today! All staff present.\n\n"
                f"🕐 Report generated on: {generated_at}"
            )
            return

        msg = (
            f"📋 <b>Daily Absent Report</b>\n"
            f"📅 {summary['date']}\n\n"
            f"👥 Total: {summary['total']} | "
            f"✅ Present: {summary['present_count']} | "
            f"❌ Absent: {summary['absent_count']}\n\n"
            f"<b>By Department:</b>\n" + '\n'.join(dept_lines) +
            f"\n\n🕐 Report generated on: {generated_at}"
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
            generated_at=generated_at,
        )

        caption = f'Absent list — {today.strftime("%d/%m/%Y")}'
        for fmt_key, (buf, filename) in files.items():
            if fmt_key == 'png':
                await _send_photo(bot, buf, caption=caption)
            else:
                await _send_doc(bot, buf, filename=filename, caption=caption)

        # Save report files to local directory if configured
        save_dir = settings.get_daily_save_dir()
        if save_dir:
            try:
                os.makedirs(save_dir, exist_ok=True)
                for fmt_key, (buf, filename) in files.items():
                    dest = os.path.join(save_dir, filename)
                    buf.seek(0)
                    with open(dest, 'wb') as f:
                        f.write(buf.read())
                    logger.info(f"Report saved to {dest}")
            except OSError as e:
                logger.error(f"Failed to save report to {save_dir}: {e}")

        # ── Email delivery (optional, controlled by [smtp] config) ───────────
        if settings.get_smtp_enabled() and settings.get_smtp_daily_enabled():
            ok, err = email_sender.send_report_email(
                sender_email=settings.get_smtp_sender_email(),
                sender_name=settings.get_smtp_sender_name(),
                app_password=settings.get_smtp_app_password(),
                recipients=settings.get_smtp_recipients(),
                subject=settings.get_smtp_subject(),
                report_date=today,
                absent=absent,
                summary=summary,
                fmt=settings.get_smtp_format(),
            )
            if ok:
                logger.info("Daily report email sent successfully.")
            else:
                logger.error(f"Daily report email failed: {err}")
                await _send(bot, f"⚠️ Daily email failed: {err}")

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
                f"🏷️ {p['badge']}  👤 {p['name']}\n"
                f"🕐 {p['time']}  📡 {dev}"
            )
            _last_live_punch_ts = p['timestamp']
    except Exception as e:
        logger.error(f"Live punch check error: {e}")


# ─── Scheduled MDB backup ─────────────────────────────────────────────────────

_TG_MAX_DOC_BYTES = 49 * 1024 * 1024   # 49 MB — same limit as bot.py


async def send_scheduled_backup(bot: Bot, triggered_by: str = 'scheduler'):
    """
    Perform an MDB backup according to the configured delivery method.
    Supports: telegram, mail, copy, or any combination.
    Logs a summary to Telegram chat and to the Python logger.
    """
    try:
        info       = mdb_reader.get_mdb_info()
        local_path = info.get('local_path', '')
        if not info.get('accessible') or not local_path or not os.path.isfile(local_path):
            await _send(bot, f'⚠️ Backup ({triggered_by}): MDB is not accessible.')
            return

        stamp      = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename   = f"mdb_backup_{stamp}.mdb"
        method     = settings.get_backup_method()
        size_bytes = os.path.getsize(local_path)
        results: list = []

        do_tg   = method in ('tg', 'tm', 'tc', 'all')
        do_mail = method in ('mail', 'mc', 'tm', 'all')
        do_copy = method in ('copy', 'mc', 'tc', 'all')

        # ── Telegram delivery ──
        if do_tg:
            if size_bytes > _TG_MAX_DOC_BYTES:
                results.append(
                    f'⚠️ Telegram skipped — {size_bytes / (1024 * 1024):.1f} MB > 49 MB limit'
                )
            else:
                try:
                    with open(local_path, 'rb') as fh:
                        buf = BytesIO(fh.read())
                    await _send_doc(
                        bot, buf, filename,
                        caption=f'📦 Scheduled MDB backup ({triggered_by})',
                    )
                    results.append('✅ Sent via Telegram')
                except Exception as e:
                    results.append(f'❌ Telegram failed: {e}')

        # ── Email delivery ──
        if do_mail:
            try:
                ok, err = email_sender.send_backup_email(
                    sender_email=settings.get_backup_sender_email(),
                    sender_name=settings.get_backup_sender_name(),
                    app_password=settings.get_backup_app_password(),
                    recipients=settings.get_backup_recipients(),
                    mdb_path=local_path,
                    filename=filename,
                )
                n = len(settings.get_backup_recipients())
                results.append(
                    f'✅ Mailed to {n} recipient(s)' if ok else f'❌ Mail failed: {err}'
                )
            except Exception as e:
                results.append(f'❌ Mail error: {e}')

        # ── Copy delivery ──
        if do_copy:
            copy_dir = settings.get_backup_copy_dir()
            if not copy_dir:
                results.append('⚠️ Copy skipped — no directory configured')
            else:
                try:
                    os.makedirs(copy_dir, exist_ok=True)
                    dest = os.path.join(copy_dir, filename)
                    shutil.copy2(local_path, dest)
                    results.append(f'✅ Copied to {dest}')
                except OSError as e:
                    results.append(f'❌ Copy failed: {e}')

        summary = ' | '.join(results) if results else '⚠️ No delivery method active'
        logger.info(f"Backup ({triggered_by}): {summary}")
        await _send(
            bot,
            f'📦 <b>MDB Backup</b> ({triggered_by})\n'
            f'📄 {filename}\n'
            f'🕐 {datetime.now().strftime("%d/%m/%Y %H:%M")}\n\n'
            + '\n'.join(f'• {r}' for r in results),
        )

    except Exception as e:
        logger.error(f"Backup error ({triggered_by}): {e}")
        await _send(bot, f'⚠️ Backup failed ({triggered_by}): {e}')


# ─── Scheduler loop ───────────────────────────────────────────────────────────

async def run_scheduler(bot: Bot):
    """
    Async scheduler loop.
    - Device online/offline alerts: every 5 minutes, always-on (not configurable).
    - Live punches: every minute, optional — toggle with /livepunches.
    - Daily report: once per day at configured time on configured days.
    - MDB backup: scheduled daily or weekly per /dbbackup → Schedule Settings.
    """
    report_sent_today = None
    backup_sent_today = None
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

            today = now.date()

            # Daily report — once per day at configured time
            report_h = settings.get_daily_hour()
            report_m = settings.get_daily_minute()

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

            # Scheduled backup
            if settings.get_backup_enabled() and backup_sent_today != today:
                backup_h    = settings.get_backup_hour()
                backup_m    = settings.get_backup_minute()
                bk_schedule = settings.get_backup_schedule()

                if bk_schedule == 'daily':
                    run_today = True
                else:  # weekly
                    backup_days = {int(d.strip())
                                   for d in settings.get_backup_days().split(',')
                                   if d.strip().isdigit()}
                    run_today = today.weekday() in backup_days

                if run_today and now.hour == backup_h and now.minute == backup_m:
                    await send_scheduled_backup(bot, triggered_by='scheduler')
                    backup_sent_today = today

        except Exception as e:
            logger.error(f"Scheduler loop error: {e}")

        await asyncio.sleep(60)
