# ZKTeco Attendance Bot

A Telegram-only attendance management bot for schools/organisations using **ZKTeco** biometric devices and **Middle East Attendance Software** (MDB/Access database).

No web dashboard, no Flask, no SQLite — pure Telegram interface.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [File Reference](#file-reference)
  - [bot.py](#botpy)
  - [mdb\_reader.py](#mdb_readerpy)
  - [zk\_devices.py](#zk_devicespy)
  - [notifier.py](#notifierpy)
  - [email\_sender.py](#email_senderpy)
  - [config.ini](#configini)
- [Setup](#setup)
- [Configuration Reference](#configuration-reference)
- [Commands](#commands)
- [Automated Notifications](#automated-notifications)
- [Notes & Caveats](#notes--caveats)

---

## Overview

The bot connects to two data sources:

| Source | Access | Purpose |
|--------|--------|---------|
| Middle East Attendance Software `.mdb` file | Read-only via `mdbtools` | Employee list, punch records, departments |
| ZKTeco biometric devices (TCP/IP) | Read/Write via `pyzk` | Device status, clock sync, reboot, user management |

All interaction happens through Telegram. Only the configured `chat_id` (and optional extra user IDs) are authorised to issue commands.

---

## Architecture

```
Telegram user
     │
     ▼
  bot.py  ──── mdb_reader.py  ──── MDB file (read-only, via mdbtools)
     │
     └────── zk_devices.py   ──── ZKTeco devices (TCP, via pyzk)
     │
     └────── notifier.py     ──── Async scheduler (daily report + device alerts)
     │
     └────── email_sender.py ──── Optional Gmail SMTP delivery (disabled by default)
```

- `bot.py` is the entry point. It creates the `python-telegram-bot` Application, registers all command handlers, and launches the background scheduler.
- `mdb_reader.py` wraps `mdb-export` (from `mdbtools`) to read employee, attendance, and department data.
- `zk_devices.py` opens TCP connections to each ZKTeco device using `pyzk`.
- `notifier.py` runs as an async loop alongside the bot, firing scheduled messages.
- `email_sender.py` is an **optional** Gmail SMTP module. It is disabled by default and does not affect any existing Telegram functionality.

---

## File Reference

### bot.py

Main entry point. Handles all Telegram command routing and formatting.

#### Auth

| Function | Description |
|----------|-------------|
| `_allowed(update)` | Returns `True` if the message sender's chat/user ID matches `CHAT_ID` or `allowed_users` in config. All handlers call this first. |
| `_deny(update)` | Sends `⛔ Unauthorized.` and returns. |

#### Formatting helpers

| Function | Description |
|----------|-------------|
| `_dept_bar(present, total)` | Renders `🟢/🟡/🔴 present/total (pct%)` for a department. |
| `_calendar_emoji(day)` | Returns `🟩` (present), `🟥` (absent), or `⬛` (weekend) for a single day. |
| `_fmt_calendar(cal)` | Renders a full monthly attendance calendar as an emoji grid with a summary header. |
| `_fmt_devices(statuses)` | Renders device status list with online/offline icons, user count, and device time. |
| `_fmt_today(summary, mode)` | Renders today's attendance in three modes: `'full'` (summary + dept breakdown), `'absent'` (absent list), `'present'` (present list). |
| `_split(text, limit=4000)` | Splits a long string into chunks ≤ 4000 characters (Telegram message limit) on line boundaries. |

#### Command handlers

All handlers check `_allowed()` before acting. They call into `mdb_reader` or `zk_devices` and reply with formatted HTML.

| Handler | Command | Description |
|---------|---------|-------------|
| `cmd_start` | `/start` | Welcome message with link to `/help`. |
| `cmd_help` | `/help` | Lists all available commands grouped by category. |
| `cmd_today` | `/today` | Fetches today's summary (total / present / absent + per-department breakdown). |
| `cmd_absent` | `/absent` | Lists today's absent employees (name + department), split across messages if needed. |
| `cmd_present` | `/present` | Lists today's present employees (name + department). |
| `cmd_late` | `/late` | Lists employees whose first punch today was after shift start, sorted by minutes late (descending). |
| `cmd_early` | `/early` | Lists employees whose first punch today was before shift start, sorted by minutes early. |
| `cmd_whoisin` | `/whoisin` | Lists employees believed to be currently inside the building (odd punch count today = checked in but not out). |
| `cmd_feed` | `/feed` | Shows the 20 most recent punches today. |
| `cmd_week` | `/week` | Day-by-day present/absent counts for the current week (Sun → today, UAE calendar). |
| `cmd_month` | `/month` | Per-department attendance percentage for the month to date. |
| `cmd_topabsent` | `/topabsent` | Top 10 most-absent employees for the current month. |
| `cmd_dept` | `/dept <name>` | Today's present/absent breakdown for matching department name(s). |
| `cmd_history` | `/history DD/MM/YYYY DD/MM/YYYY` | Day-by-day attendance summary for a custom date range (max 31 days). |
| `cmd_syncrange` | `/syncrange DD/MM/YYYY DD/MM/YYYY` | Read-only range summary (no write-back/sync to MDB). |
| `cmd_trend` | `/trend [days]` | Attendance trend over the last working days (default 14). |
| `cmd_report` | `/report` | Triggers `notifier.send_daily_report()` on demand — sends today's absent list as an XLSX file. |
| `cmd_search` | `/search <name or badge>` | Searches employee records by name or badge number (partial match, case-insensitive). Returns up to 20 results with active/inactive status. |
| `cmd_punches` | `/punches <badge>` | Lists today's punch times for a specific employee, labelled `→ IN` / `← OUT` by order. |
| `cmd_employeereport` | `/employeereport <badge>` | Month-to-date read-only attendance report for one employee. |
| `cmd_calendar` | `/calendar <badge> [YYYY-MM]` | Renders a full-month emoji calendar grid for an employee. Defaults to the current month. |
| `cmd_devices` | `/devices` | Pings all configured ZKTeco devices and reports online status, user count, and device clock. |
| `cmd_clocksync` | `/clocksync` | Sets the clock on every device to the current system time. |
| `cmd_reboot` | `/reboot <ip or name>` | Reboots a single device (lookup by IP or human-readable name). |
| `cmd_usersync` | `/usersync` | Collects users from all online devices and pushes any missing users to each device. |
| `cmd_adduser` | `/adduser <badge> <full name>` | Creates a new user record on every device. Biometric enrollment must be done physically. |
| `cmd_unknown_users` | `/unknown` | Finds device users whose badge number is not present in the MDB employee list. |
| `cmd_stats` | `/stats` | Shows MDB accessibility, file size, modification time, and employee counts. |
| `cmd_mdbinfo` | `/mdbinfo` | Shows the configured and resolved MDB paths plus file metadata. |
| `cmd_setmdb` | `/setmdb <path>` | Updates the MDB path in `config.ini` at runtime and immediately tests accessibility. |
| `cmd_tables` | `/tables` | Lists all tables in the MDB file (diagnostic). |
| `cmd_download` | `/download <ip>` | Read-only device snapshot and recent linked MDB punches (no download/write action). |
| `cmd_dbbackup` | `/dbbackup` | Sends a read-only copy of the MDB file. |
| `cmd_importcsv` | `/importcsv` | CSV validation/preview only (no MDB import). |
| `cmd_autonmap` | `/autonmap` | Suggests UID→badge matches only (not persisted). |
| `cmd_shifts` | `/shifts` | Read-only shift configuration view. |
| `cmd_workdays` | `/workdays` | Read-only workday/report-days configuration view. |
| `cmd_editemail` | `/editemail` | Opens the Gmail SMTP settings panel (inline keyboard). Configure sender, App Password, recipients, subject, format, and schedule. Disabled by default — pure Telegram users are unaffected. |
| `cmd_mail` | `/mail` | Interactive prompt to send an attendance report by email — choose **Today** or **Pick Date** (calendar picker). Requires SMTP to be enabled via `/editemail`. |
| `unknown_cmd` | *(any other command)* | Replies with `❓ Unknown command. Send /help for list.` |

#### Main entry

| Function | Description |
|----------|-------------|
| `post_init(app)` | `post_init` hook — launches `notifier.run_scheduler()` as an asyncio task after the bot initialises. |
| `main()` | Validates config, builds the `Application`, registers all handlers, and starts polling. |

---

### mdb\_reader.py

Read-only interface to the Middle East Attendance Software `.mdb` (Access) database via the `mdbtools` CLI (`mdb-export`, `mdb-tables`).

**MDB tables used:**

| Table | Key columns | Purpose |
|-------|-------------|---------|
| `USERINFO` | `USERID`, `Badgenumber`, `Name`, `DEFAULTDEPTID`, `ATT` | Employee master list |
| `CHECKINOUT` | `USERID`, `CHECKTIME`, `SENSORID` | All punch records |
| `DEPARTMENTS` | `DEPTID`, `DEPTNAME` | Department name lookup |

#### Config & path helpers

| Function | Description |
|----------|-------------|
| `_get_mdb_path()` | Returns the raw MDB path string from config. |
| `_get_excluded_depts()` | Returns the list of department names to exclude from reports (uppercased). |
| `set_mdb_path(new_path)` | Updates `[mdb] path` in `config.ini` on disk and invalidates the department cache. |
| `_resolve_local_path()` | Converts the configured path to a locally accessible file path — handles direct paths and UNC (`//server/share/...`) via a configured mount point. |

#### Core MDB access

| Function | Description |
|----------|-------------|
| `_mdb_export(table)` | Runs `mdb-export` and returns a list of dicts (one per row). Raises `RuntimeError` if the MDB is not accessible. |
| `_csv_split(line)` | Parses a single CSV line, respecting quoted fields with commas. |
| `_parse_dt(s)` | Tries multiple datetime formats to parse a `CHECKTIME` string into a `datetime` object. |
| `list_tables()` | Returns all table names from the MDB via `mdb-tables`. |
| `get_mdb_info()` | Returns a dict with configured path, resolved path, accessibility, file size (MB), and last-modified timestamp. |

#### Department cache

| Function | Description |
|----------|-------------|
| `_get_dept_map()` | Returns a `DEPTID → DEPTNAME` dict, loading from the MDB on first call and caching in-process. |
| `refresh_dept_cache()` | Clears and reloads the department cache (called automatically after `set_mdb_path`). |

#### Employee queries

| Function | Description |
|----------|-------------|
| `get_employees(active_only=True)` | Returns all employees from `USERINFO` as a list of dicts (`uid`, `badge`, `name`, `dept`, `active`). Excludes departments in `[departments] exclude` and, when `active_only=True`, employees with `ATT=0`. |
| `search_employee(query)` | Case-insensitive partial match on name or badge number across all employees (including inactive). |

#### Attendance queries

| Function | Description |
|----------|-------------|
| `get_attendance(date_from, date_to, uid=None, badge=None)` | Returns punch records from `CHECKINOUT` filtered by date range and optionally by `uid` or `badge`. If `badge` is given, it resolves to `uid` first. |
| `_uid_map()` | Returns a `uid → employee dict` lookup for all employees. |

#### Summary / report functions

| Function | Description |
|----------|-------------|
| `get_today_summary()` | Returns a summary dict for today: `date`, `present`/`absent` employee lists, counts, `dept_stats`, and raw `punches`. |
| `get_history(date_from, date_to)` | Returns a list of daily summary dicts for a date range, each with present/absent employee lists, counts, weekday name, and `is_weekend` flag. |
| `get_employee_calendar(badge, year, month)` | Returns a monthly calendar dict for one employee with per-day presence, punch times, and summary counts (present/absent/working days). |
| `get_late_today(shift_start=None)` | Returns employees whose first punch today was after `shift_start` (default from config `07:30`), sorted by minutes late descending. |
| `get_top_absent(n=10, date_from=None, date_to=None)` | Returns the top `n` most-absent active employees for the period (default: current month to date). |
| `get_who_is_in()` | Returns employees with an odd punch count today (checked in but not yet out). |
| `get_punch_feed(n=20)` | Returns the `n` most recent punches today with employee name and department resolved. |
| `get_week_summary()` | Returns day-by-day history from the most recent Sunday to today (UAE Sun–Thu work week). |
| `get_month_dept_summary()` | Returns per-department attendance percentages for the current month to date, sorted ascending by attendance rate. |
| `get_db_stats()` | Returns MDB metadata plus total and active employee counts. |
| `get_employee_punches(badge, date_from, date_to)` | Convenience wrapper — returns punch records for a badge over a date range. |

---

### zk\_devices.py

Controls ZKTeco biometric devices over TCP using the `pyzk` library.

#### Config helpers

| Function | Description |
|----------|-------------|
| `_get_devices()` | Parses `[devices]` from config and returns a list of `{ip, name, port, timeout}` dicts. |
| `get_device_by_ip(ip)` | Returns the device dict for a given IP, or `None`. |
| `get_device_by_name(name)` | Case-insensitive lookup of a device by its human-readable name. |

#### Connection

| Function | Description |
|----------|-------------|
| `_connect(device)` | Opens a TCP connection to a ZK device and returns `(conn, zk)`. Raises on network error. |

#### Device operations

| Function | Description |
|----------|-------------|
| `get_device_status()` | Connects to every device, retrieves firmware version, user count, record count, and current clock. Returns a list of status dicts (online/offline). |
| `sync_clocks()` | Sets every device's internal clock to the current system time (`datetime.now()`). |
| `reboot_device(ip)` | Reboots a single device identified by IP or name. Returns `{ok, name, ip}` or an error dict. |
| `sync_users()` | Two-phase user sync: (1) collects the union of all users from all online devices, (2) pushes any user missing from a device to that device. |
| `add_user(badge, name, privilege=0, target_ips=None)` | Adds a new user (badge number as `user_id`, name truncated to 24 chars) to all devices or a subset. Privilege levels: 0=user, 2=enroller, 6=manager, 14=admin. |
| `get_unknown_users(known_badges)` | Returns device users whose `user_id` (badge) is not in `known_badges`. Used by `/unknown` to find orphaned device records. |

---

### notifier.py

Async scheduler that runs alongside the bot, sending proactive Telegram notifications.

#### Config helpers

| Function | Description |
|----------|-------------|
| `_chat_id()` | Returns the target Telegram chat ID from config. |
| `_bot_token()` | Returns the bot token from config. |
| `_notify_device_status()` | Returns `True` if device online/offline alerts are enabled. |
| `_notify_punches()` | Returns `True` if per-punch notifications are enabled (default: off). |
| `_report_time()` | Returns `(hour, minute)` for the scheduled daily report (default: 08:10). |

#### Send helpers

| Function | Description |
|----------|-------------|
| `_send(bot, text, parse_mode='HTML')` | Sends a text message to the configured chat ID. |
| `_send_doc(bot, data, filename, caption='')` | Sends a `BytesIO` document to the configured chat ID. |

#### Scheduled tasks

| Function | Description |
|----------|-------------|
| `send_daily_report(bot)` | Builds today's absent list as an in-memory XLSX (using `pandas` + `openpyxl`) and sends it with a summary message. If no absences, sends a "✅ All present" message. Also called on demand by `/report`. If SMTP is enabled and `daily_email_enabled = 1`, also calls `email_sender.send_report_email()`. |
| `check_device_status_changes(bot)` | Compares each device's current online/offline state to the last known state. Sends a `🟢 ONLINE` or `🔴 OFFLINE` alert only when the state changes. Silent on first run (just records initial state). |
| `run_scheduler(bot)` | Async loop (runs every 60 s): checks device status every 5 minutes and fires `send_daily_report` once per day at the configured time. |

---

### email\_sender.py

Optional Gmail SMTP module. All functions use only the Python standard library (`smtplib`, `email`). **Disabled by default** — if `[smtp] enabled = 0` in `config.ini`, nothing in this module is ever called.

#### Body builders

| Function | Description |
|----------|-------------|
| `build_plain_body(report_date, absent, summary)` | Builds a plain-text email body for the absent report (dept-grouped list). |
| `build_html_body(report_date, absent, summary)` | Builds a styled HTML email body with a summary header and per-department absent table. |

#### Send function

| Function | Description |
|----------|-------------|
| `send_report_email(sender_email, sender_name, app_password, recipients, subject, report_date, absent, summary, fmt)` | Authenticates to Gmail SMTP (TLS, port 587) and delivers the absent report email. `fmt` is `'html'`, `'plain'`, or `'both'` (multipart/alternative). Returns `(True, '')` on success or `(False, error_message)` on failure. |

---

### config.ini

All runtime settings. Edit before first run.

```ini
[telegram]
bot_token     = YOUR_BOT_TOKEN        # from @BotFather
chat_id       = YOUR_CHAT_ID          # Telegram user/group to authorise
allowed_users =                       # comma-separated extra user IDs (optional)

[mdb]
path          = /mnt/attdb/att.mdb    # local path, UNC, or mounted path to .mdb file
smb_user      =                       # SMB credentials (leave blank for guest)
smb_pass      =
smb_domain    = WORKGROUP
mount_point   = /mnt/attdb            # local mount point for UNC paths

[devices]
ips     = 10.20.141.21,10.20.141.22   # comma-separated ZKTeco device IPs
names   = Girls 2,Boys 2              # human-readable names (same order as ips)
port    = 4370                        # ZKTeco default port
timeout = 10                          # connection timeout (seconds)

[departments]
exclude = DELETED EMPLOYEES,TRANSPORT # departments excluded from reports

[attendance]
shift_start = 07:30                   # late-arrival threshold (HH:MM, 24h)

[notifications]
notify_punches       = 0              # 1 = send Telegram message per punch (noisy)
notify_device_status = 1              # 1 = alert on device online/offline change
daily_report_hour    = 8              # daily absent report time
daily_report_minute  = 10

[reports]
export_dir = /tmp/zk_reports          # temp dir for XLSX exports

[smtp]
enabled              = 0              # 1 = enable SMTP email delivery
daily_email_enabled  = 0              # 1 = also email the scheduled daily report
sender_email         =                # Gmail address to send from
sender_name          = ZKTeco Attendance Bot  # From: display name
app_password         =                # Gmail App Password (not account password)
recipients           =                # comma-separated recipient addresses
subject              = Daily Absent Report - {date}  # {date} is replaced at send time
format               = html           # html | plain | both (multipart/alternative)
```

> **SMTP is disabled by default (`enabled = 0`).** All existing Telegram functionality is unchanged. Enable it by running `/editemail` in the bot.
>
> **Gmail App Password:** create one at *Google Account → Security → 2-Step Verification → App Passwords*. Use the 16-character password here — never your main Google account password.

---

## Setup

### 1. System packages (WSL / Ubuntu / Raspberry Pi)

```bash
sudo apt update
sudo apt install -y mdbtools smbclient cifs-utils python3-pip
```

### 2. Python packages

```bash
pip install -r requirements.txt
```

**Dependencies:**

| Package | Version | Purpose |
|---------|---------|---------|
| `python-telegram-bot` | ≥ 20.7 | Telegram Bot API (async) |
| `pyzk` | ≥ 0.9 | ZKTeco device communication |
| `pandas` | ≥ 2.0.0 | XLSX report generation |
| `openpyxl` | ≥ 3.1.0 | Excel writer backend for pandas |

### 3. Mount the MDB share

```bash
sudo mkdir -p /mnt/attdb

# One-time mount:
sudo mount -t cifs //10.20.141.17/d /mnt/attdb -o guest,ro,nounix,vers=2.0

# Persistent (add to /etc/fstab):
# //10.20.141.17/d  /mnt/attdb  cifs  guest,ro,nounix,vers=2.0  0  0
```

### 4. Configure

```bash
nano config.ini
```

At minimum, set:
- `[telegram] bot_token` — get from [@BotFather](https://t.me/BotFather)
- `[telegram] chat_id` — your user or group ID
- `[mdb] path` — path to the `.mdb` file
- `[devices] ips` and `[devices] names`

### 5. Run

```bash
python3 bot.py
```

### 6. Auto-start on boot (systemd)

```bash
sudo nano /etc/systemd/system/zkbot.service
```

```ini
[Unit]
Description=ZKTeco Telegram Bot
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/zk_tg_bot
ExecStart=/usr/bin/python3 /path/to/zk_tg_bot/bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable zkbot
sudo systemctl start zkbot
sudo journalctl -u zkbot -f    # live logs
```

---

## Commands

### Attendance

| Command | Description |
|---------|-------------|
| `/today` | Present / absent totals with per-department breakdown |
| `/absent` | Full absent employee list (name + department) |
| `/present` | Full present employee list |
| `/late` | Employees whose first punch was after shift start, sorted by minutes late |
| `/early` | Employees whose first punch was before shift start, sorted by minutes early |
| `/whoisin` | Employees currently inside (checked in but not checked out) |
| `/feed` | Last 20 punches today |
| `/week` | Day-by-day summary for the current work week (Sun → today) |
| `/month` | Per-department attendance percentage, month to date |
| `/topabsent` | Top 10 most-absent employees this month |
| `/dept <name>` | Present/absent breakdown for matching department name(s) |
| `/history DD/MM/YYYY DD/MM/YYYY` | Day-by-day attendance for a custom range (max 31 days) |
| `/syncrange DD/MM/YYYY DD/MM/YYYY` | Read-only range summary (no write-back to MDB) |
| `/trend [days]` | Attendance trend over latest working days (default 14) |
| `/report` | Send today's absent list as an XLSX file |

### Employee

| Command | Description |
|---------|-------------|
| `/search <name or badge>` | Search employees by name or badge (partial match) |
| `/punches <badge>` | Today's punch times for one employee, labelled IN/OUT |
| `/employeereport <badge>` | Month-to-date read-only attendance report for one employee |
| `/calendar <badge> [YYYY-MM]` | Monthly emoji attendance calendar for one employee |

### Devices

| Command | Description |
|---------|-------------|
| `/devices` | Status of all ZKTeco devices (online/offline, user count, clock) |
| `/clocksync` | Sync all device clocks to current system time |
| `/reboot <ip or name>` | Reboot a single device |
| `/usersync` | Sync users across all devices (push missing users to each device) |
| `/adduser <badge> <full name>` | Add a new user to all devices |
| `/unknown` | List device users whose badge is not in the MDB |

### Database

| Command | Description |
|---------|-------------|
| `/stats` | MDB accessibility, size, modification date, employee counts |
| `/mdbinfo` | Configured and resolved MDB path with file metadata |
| `/setmdb <path>` | Update MDB path at runtime (no restart needed) |
| `/tables` | List all tables in the MDB (diagnostic) |
| `/download <ip>` | Read-only device snapshot + latest linked MDB punches (no write action) |
| `/dbbackup` | Send a read-only copy of the MDB file |
| `/importcsv` | Validate/preview uploaded CSV only; does not import into MDB |
| `/autonmap` | Show UID→badge mapping suggestions only; does not persist |
| `/shifts` | Read-only view of shift settings used by late/early checks |
| `/workdays` | Read-only view of configured report days and fixed weekend rules |

### Settings

| Command | Description |
|---------|-------------|
| `/livepunches` | Toggle per-punch live notifications on/off |
| `/editreport` | Interactive panel — configure on-demand `/report` settings (departments, format, template, save dir) |
| `/editdaily` | Interactive panel — configure scheduled daily report settings (time, days, departments, format, save dir) |
| `/editemail` | Interactive panel — configure Gmail SMTP email delivery (see below) |
| `/mail` | Send attendance report by email — **Today** button or interactive date-picker calendar |

#### `/editemail` — Gmail SMTP settings

All email configuration is managed entirely through this Telegram command — no manual `config.ini` editing is required.

| Setting | Button | Description |
|---------|--------|-------------|
| SMTP on/off | 🔌 SMTP On/Off | Master switch. Off by default — disabling this leaves all Telegram behaviour unchanged. |
| Daily email on/off | 📅 Daily On/Off | When on, the scheduled daily report is also sent by email. |
| Sender email | 📤 Sender Email | Gmail address the bot sends from. |
| Sender name | 👤 Sender Name | Display name that appears in the `From:` header. |
| App Password | 🔑 Password | Gmail App Password (16-char, obtained from Google Account → Security). Never stored outside `config.ini`. |
| Recipients | 👥 Recipients | Add or remove email addresses. Tap ❌ next to an address to remove it. |
| Subject | 📝 Subject | Email subject line; `{date}` is replaced with the report date at send time. |
| Format | 📄 Format | `HTML` (styled table), `PLAIN` (plain text), or `BOTH` (multipart/alternative with both). |
| Send Now | 📨 Send Now | Immediately send the today's absent report email to all configured recipients. Useful for testing. |

---

## Automated Notifications

The `notifier.run_scheduler()` loop runs in the background as an asyncio task:

| Trigger | Action |
|---------|--------|
| Every 5 minutes | Check all device online/offline states; alert in Telegram if any device changes state |
| Daily at 08:15 (configurable) | Send absent employee list as a text summary + XLSX attachment to Telegram |
| Daily at 08:15 (configurable, optional) | If `[smtp] enabled = 1` and `daily_email_enabled = 1`, also send the absent report by email |

Report timing is configurable via `/editdaily`. Email delivery is configurable via `/editemail`.

---

## Notes & Caveats

- **MDB is read-only.** Middle East Attendance Software remains the single source of truth. The bot never writes to the database.
- **Read-only adaptations.** `/importcsv`, `/autonmap`, `/download`, `/syncrange`, `/shifts`, and `/workdays` are implemented as read-only Telegram views/previews only; they do not persist changes.
- **Backups are file copies only.** `/dbbackup` sends a copy of the MDB file for safekeeping and does not mutate the original MDB.
- **Device user writes.** `/adduser` and `/usersync` write to ZKTeco devices only. Middle East Software picks up new users on its next "Download User Info" sync.
- **Biometric enrollment** (fingerprint / face) must be performed physically on the device after adding a user.
- **Weekend** = Friday + Saturday (UAE / Gulf calendar). Saturday (`weekday() == 5`) and Friday (`weekday() == 4`) are marked as weekends in all reports.
- **Who-is-in logic** uses odd punch count as a proxy for "currently inside." This may be inaccurate if a device recorded an extra erroneous punch.
- **Telegram message limit.** The bot splits responses longer than 4000 characters across multiple messages automatically.
- **Authentication.** Only the `chat_id` specified in config (plus any `allowed_users`) can issue commands. All other senders receive `⛔ Unauthorized.`
- **Email is opt-in.** SMTP delivery defaults to disabled (`[smtp] enabled = 0`). Pure Telegram users need not configure it and will see no change in behaviour. Use `/editemail` to enable it.
- **Gmail App Password.** Standard Gmail passwords do not work. Enable 2-Step Verification on your Google account and generate an App Password at *Google Account → Security → App Passwords*. The password is stored in `config.ini` — treat that file as sensitive.
- **No new Python dependencies** are required for email. `smtplib` and `email` are part of the Python standard library.
