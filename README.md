<div align="center">

# Squawk

**A Discord bot that posts stock and market news from Yahoo Finance and GlobalNewswire to a per-server ticker watchlist.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
![Version](https://img.shields.io/badge/version-1.0.0-orange)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![discord.py](https://img.shields.io/badge/discord.py-2.4%2B-5865F2)

</div>

## Self-hosting

### Requirements

- Python 3.10+

### Installation

```bash
git clone https://github.com/psalm2517/squawk-bot.git
cd squawk-bot
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set `DISCORD_TOKEN` to your bot's token. Optional:

- `ALERT_USER_ID` - your Discord user ID, DMed when a feed enters failure backoff.
- `MAX_TICKERS_PER_SERVER` - cap on total tickers tracked per server (leave blank for no cap).

### Running

```bash
python bot.py
```

For production, run under a process supervisor such as `systemd`. An example unit file is included in the [Running as a service](#running-as-a-service) section below.

### First-time server setup

Once the bot is in your Discord server, an admin should run:

```
/watchlist channel action:set channel:#your-channel
/market channel action:set channel:#your-channel
```

Then add tickers with `/watchlist ticker action:add ticker:AAPL,MSFT,...`.

## Commands

**Manage Server** members can use every command from any channel, cooldown-free. All setup and mutating commands (marked *admin* below) require Manage Server. The read-only commands are open to everyone, but you can restrict which channels they can be used in via `/config channel`.

| Command | Description | Cooldown |
|---|---|---|
| `/watchlist ticker action:<add\|remove> ticker:TICKER` | *admin* - Add/remove tickers (comma-separated). | - |
| `/watchlist channel action:<set\|clear>` | *admin* - Set the channel ticker news posts to. | - |
| `/watchlist show` | List tracked tickers. | 60s |
| `/ticker recent ticker:TICKER` | Fetch the 3 most recent articles for any ticker. | 60s |
| `/market channel action:<set\|clear>` | *admin* - Set the channel market news posts to. | - |
| `/market recent` | Fetch the 3 most recent market news articles. | 60s |
| `/config show` | *admin* - Show this server's full configuration (ephemeral). | - |
| `/config channel mode:<all\|none> exceptions:<#a #b …>` | *admin* - Set where regular users can use read-only commands. `all` = allowed everywhere; `none` = blocked everywhere. Exceptions flip the rule for the listed channels. Pass either param on its own, or both. | - |
| `/config blacklist action:<add\|remove\|show\|clear> pattern:text` | *admin* - Skip articles whose URL contains a given substring (e.g. `trefis`). | - |
| `/squawk` | Show bot version, uptime, tickers tracked, last poll, and any feeds in backoff. | 5s |

## How it works

Every 2 minutes, Squawk fetches the Yahoo Finance RSS feed for each tracked ticker and posts any unseen articles. Market news is pulled from major index feeds (S&P 500, Dow, Nasdaq, Russell 2000). Articles are deduplicated per-server by normalized URL (tracking params stripped). When a ticker is first added, existing articles are marked seen so there's no backlog dump.

Feed failures back off automatically after 3 consecutive errors and alert the server owner (and `ALERT_USER_ID` if set) via DM. A watchdog force-exits the process if the poll loop hangs, letting systemd restart it.

## Storage

All state lives in flat JSON files (no database), created automatically and gitignored:

| File | Contents |
|---|---|
| `watchlist.json` | Per-guild ticker lists |
| `seen.json` | Per-guild seen article URLs (normalized) |
| `config.json` | Per-guild ticker news channel |
| `news_config.json` | Per-guild market news channel |
| `permissions.json` | Per-guild read-only channel mode and exception list |
| `blacklist.json` | Per-guild URL substring blocklist |

## Running as a service

Example `systemd` unit file (`/etc/systemd/system/squawk.service`):

```ini
[Unit]
Description=Squawk Discord Bot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/squawk
ExecStart=/opt/squawk/venv/bin/python /opt/squawk/bot.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and manage:

```bash
systemctl enable --now squawk
systemctl status squawk
journalctl -u squawk -f
```

## License

Squawk is licensed under the [MIT License](./LICENSE).

<div align="center">

</div>
