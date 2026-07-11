![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Version](https://img.shields.io/badge/version-0.1.0--dev-orange)
![License](https://img.shields.io/github/license/yerettegroup/squawk-bot)
![discord.py](https://img.shields.io/badge/discord.py-2.4%2B-5865F2)

# Squawk

A Discord bot that polls Yahoo Finance RSS feeds for a per-server stock ticker watchlist and general market news, posting new articles as `[Headline](<link>)` messages to configured channels.

## Setup

1. Create a Discord app at [discord.dev](https://discord.dev). No privileged intents required.
2. Invite the bot with the `bot` and `applications.commands` scopes, plus `Send Messages` permission.
3. Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN`. `ALERT_USER_ID` is optional — set it to your Discord user ID to receive a DM when a feed starts failing.
4. Install dependencies: `pip install -r requirements.txt`
5. Run: `python bot.py`
6. In each server, use `/watchlist channel` and `/market channel` to configure where posts go.

## Commands

All commands require **Manage Server** or the server's configured allowed role. Manage Server users bypass cooldowns and channel restrictions entirely.

| Command | Description | Cooldown |
|---|---|---|
| `/watchlist ticker action:<add\|remove> ticker:TICKER` | Add/remove tickers (comma-separated, max 25). | 60s |
| `/watchlist channel action:<set\|clear>` | Set the channel ticker news posts to. | 15s |
| `/watchlist show` | List tracked tickers. | 60s |
| `/ticker recent ticker:TICKER` | 3 most recent articles for any ticker. | 30s |
| `/market channel action:<set\|clear>` | Set the channel market news posts to. | 15s |
| `/market recent` | 3 most recent market news articles. | 30s |
| `/config role action:<set\|clear> role:@role` | Delegate command access to a role. | 15s |
| `/config channel action:<add\|remove\|show\|clear>` | Restrict which channels commands can be used in. | 15s |
| `/config blacklist action:<add\|remove\|show\|clear> pattern:text` | Block articles from URLs containing a substring (e.g. `trefis`). | 15s |
| `/squawk` | Show bot status, uptime, config, and last poll time. | 5s |

`/config` commands always require real Manage Server permission — the delegated role cannot use them.

## How it works

Every 5 minutes, Squawk fetches the Yahoo Finance RSS feed for each tracked ticker and posts any unseen articles. Market news is pulled from major index feeds (S&P 500, Dow, Nasdaq, Russell 2000). Articles are deduplicated per-server by normalized URL (tracking params stripped). When a ticker is first added, existing articles are marked seen so there's no backlog dump.

Feed failures back off automatically after 3 consecutive errors and alert the server owner (and `ALERT_USER_ID` if set) via DM. A watchdog force-exits the process if the poll loop hangs, letting systemd restart it.

## Storage

All state lives in flat JSON files (no database), created automatically and gitignored:

| File | Contents |
|---|---|
| `watchlist.json` | Per-guild ticker lists |
| `seen.json` | Per-guild seen article URLs (normalized) |
| `config.json` | Per-guild ticker news channel |
| `news_config.json` | Per-guild market news channel |
| `permissions.json` | Per-guild allowed role and command channel restrictions |
| `blacklist.json` | Per-guild URL substring blocklist |

## Running as a service

```
systemctl status squawk
systemctl restart squawk
journalctl -u squawk -f
```
