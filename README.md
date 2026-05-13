# ZKTeco Attendance Bot

Telegram-only attendance bot. Reads MDB from Middle East Attendance Software.
No dashboard, no Flask, no SQLite. Pure Telegram interface.

## Files

```
bot.py          — main bot (all Telegram handlers)
mdb_reader.py   — read-only MDB access via mdbtools
zk_devices.py   — ZK device control (reboot, sync, add user)
notifier.py     — scheduled notifications (daily report, device alerts)
config.ini      — all settings
requirements.txt
```

## Setup (WSL / Ubuntu / Pi)

### 1. System packages
```bash
sudo apt update
sudo apt install -y mdbtools smbclient cifs-utils python3-pip
```

### 2. Python packages
```bash
pip install -r requirements.txt
```

### 3. Mount the MDB share (option A — persistent mount)
```bash
sudo mkdir -p /mnt/attdb
# Add to /etc/fstab for auto-mount on boot:
# //10.20.141.17/d /mnt/attdb cifs guest,ro,nounix,vers=2.0 0 0
sudo mount -t cifs //10.20.141.17/d /mnt/attdb -o guest,ro,nounix,vers=2.0
```

### 4. Configure
Edit `config.ini`:
```ini
[telegram]
bot_token = YOUR_BOT_TOKEN   # from @BotFather
chat_id   = YOUR_CHAT_ID     # your Telegram user/group ID

[mdb]
path = //10.20.141.17/d/Attendance database/attBackup23.12.25.mdb
# Or if manually mounted:
# path = /mnt/attdb/Attendance database/attBackup23.12.25.mdb
```

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
WorkingDirectory=/path/to/zk_bot
ExecStart=/usr/bin/python3 /path/to/zk_bot/bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable zkbot
sudo systemctl start zkbot
sudo journalctl -u zkbot -f   # live logs
```

## Commands

| Command | Description |
|---|---|
| /today | Present/absent + dept breakdown |
| /absent | Absent list |
| /present | Present list |
| /late | Late arrivals today |
| /whoisin | Currently inside building |
| /feed | Last 20 punches |
| /week | This week day-by-day |
| /month | Monthly dept % summary |
| /topabsent | Most absent this month |
| /history DD/MM/YYYY DD/MM/YYYY | Range report |
| /report | Send absent XLSX |
| /search name | Find employee |
| /punches badge | Today's punches for employee |
| /calendar badge [YYYY-MM] | Monthly calendar |
| /devices | All device status |
| /clocksync | Sync all clocks |
| /reboot ip\_or\_name | Reboot a device |
| /usersync | Sync users across devices |
| /adduser badge name | Add user to devices |
| /unknown | Users on devices not in MDB |
| /stats | MDB stats |
| /mdbinfo | MDB path + file info |
| /setmdb path | Change MDB path live |
| /tables | List MDB tables |

## Notes

- MDB is **read-only** — Middle East Attendance Software remains the authority
- Device control (reboot, sync) does not touch MDB
- Adding a user via /adduser writes to ZK devices only; Middle East Software picks up the user on next "Download User Info" sync
- Biometric enrollment (fingerprint/face) must be done physically on device
- Weekend = Friday + Saturday (UAE calendar)
- Daily report sent automatically at 08:10 (configurable in config.ini)
