# Squawk

A Discord bot that polls Yahoo Finance for a per-server configurable watchlist of stock
tickers, plus general market news, and posts new articles as plain-text messages
(`[Headline](<link>)`) to a designated channel in each server.

## Requirements

- **Python 3.10+** (uses `X | None` type syntax)
- The dependencies in `requirements.txt` (`discord.py`, `feedparser`, `python-dotenv`)

## Setup

1. Create a Discord app at [discord.dev](https://discord.dev) (the Discord Developer Portal).
   No privileged intents are required — the bot is slash-commands only and never reads
   message content.
2. Invite the bot to your server using the OAuth2 URL generator with the
   `bot` and `applications.commands` scopes, and the `Send Messages` +
   `Use Slash Commands` permissions.
3. Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN`. `ALERT_USER_ID` is optional
   — set it to your Discord user ID if you want a DM whenever a feed starts failing;
   leave it blank to skip that.
4. Install dependencies: `pip install -r requirements.txt`
5. Run the bot: `python bot.py`
6. In each server, run `/watchlist channel action:set channel:#your-channel` (requires
   **Manage Server** permission) to set where that server's ticker news gets posted, and
   `/market channel action:set channel:#your-channel` to enable general financial news.

Everything is configured per server via slash commands. There's no global config to edit.

## Slash Commands

All commands are scoped to the server they're run in. Every command requires either the
**Manage Server** permission or this server's configured allowed role (see `/config role`
below) — nothing is open to everyone by default. Manage Server members can always use
every command from any channel, even if a channel restriction is configured (see
`/config channel`) — this is a deliberate safety valve so an admin can never lock
themselves out. Each command has its own per-user cooldown (noted below).

### `/watchlist` — stock ticker watchlist

- `/watchlist ticker action:<add|remove> ticker:TICKER` — add/remove ticker(s), auto-
  uppercased. Comma-separate (no spaces) to do several at once, e.g.
  `ticker:AAPL,MSFT,BRK-B` (max 25 per call). The reply summarizes what was added/removed
  vs. skipped (already-tracked, invalid, or not-found). Cooldown: 120s.
- `/watchlist channel action:<set|clear> channel:#channel` — set or clear the channel
  ticker news posts to. Cooldown: 30s.
- `/watchlist show` — list tickers currently tracked in this server. Cooldown: 120s.

### `/config` — server-wide permissions & filtering

- `/config role action:<set|clear> role:@role` — set or clear a role allowed to use
  Squawk's restricted commands, in addition to Manage Server. Cooldown: 30s.
- `/config channel action:<add|remove|show|clear> channel:#channel` — restrict which
  channel(s) Squawk's commands can be used in (by non-Manage-Server users). `add`/`remove`
  need a channel, `show` lists the current restriction, `clear` removes it entirely
  (default: no restriction, any channel allowed). Cooldown: 30s.
- `/config blacklist action:<add|remove|show|clear> pattern:text` — block articles whose
  link URL contains a given case-insensitive substring, e.g. `pattern:trefis` to drop a
  spammy source. Applies to both auto-posts and the `recent` lookups. Cooldown: 30s.

All `/config` commands **always require real Manage Server permission**, never the
delegated role — otherwise someone holding only the delegated role could grant it to
others or lock everyone (including admins from the wrong channel) out, which would be a
privilege-escalation path.

### `/ticker` — on-demand lookup for any ticker

- `/ticker recent ticker:TICKER` — the 3 most recent articles for any ticker, whether or
  not it's on this server's watchlist. Independent of the poll loop/`seen.json`. Cooldown: 60s.

### `/market` — general market news (not tied to a ticker)

- `/market channel action:<set|clear> channel:#channel` — enable/disable general market
  news and set where it posts. Cooldown: 30s.
- `/market recent` — on-demand lookup of the 3 most recent general market news articles.
  Cooldown: 60s.

### `/squawk` — status

Shows the bot's version, uptime, the configured ticker/news channels, number of tickers
tracked, time of the last poll, and any feeds currently in fetch-failure backoff.
Cooldown: 10s.

## Reliability

- If a feed fails 3 times in a row and enters backoff, the owner of every affected server
  gets DMed once for that failure episode (not spammed every retry). If `ALERT_USER_ID`
  is set in `.env`, that user gets DMed too — useful for whoever operates the bot to hear
  about failures without being a member of every server it's in. Optional; leave it unset
  to skip the operator DM entirely.
- A watchdog checks every 2 minutes that the poll loop is still completing; if it's been
  stuck for 3x the poll interval, the process force-exits so systemd's
  `Restart=on-failure` brings it back up. This is on top of systemd already restarting on
  an outright crash.
- All JSON state files are written atomically (temp file + rename) so a kill mid-write
  can't corrupt them.

## How it works

Every 5 minutes, Squawk fetches:

- The Yahoo Finance RSS feed for each unique ticker across every server's watchlist
  (fetched once even if multiple servers track the same ticker):
  `https://feeds.finance.yahoo.com/rss/2.0/headline?s=TICKER&region=US&lang=en-US`
- The market news feed (fetched once if any server has it enabled) — major indices
  (S&P 500, Dow, Nasdaq, Russell 2000) via the same per-ticker mechanism, which skews
  results toward genuinely broad market coverage rather than single-stock stories. A
  filter additionally drops templated single-stock-vs-market blurbs that still show up
  in that feed (see `CHANGELOG.md` for details).

For each server, any article that hasn't already been posted in that server (tracked
per-server in `seen.json`, shared between ticker and market news) and isn't blacklisted
is posted as a plain-text message — `[Headline](<link>)` — to the relevant configured
channel, linking to the original source article rather than Yahoo Finance. No embed, no
summary, no footer. Each server's seen list is capped at 1000 entries, dropping the
oldest once the cap is reached.

De-duplication is by a **normalized** URL: tracking query params (`utm_*`, `.tsrc`, etc.)
are stripped before comparison, so the same article arriving with different tracking tags
isn't re-posted. This does **not** catch the same story syndicated under genuinely
different URLs/domains — that would require fuzzy title matching, which is too
false-positive-prone to enable by default. Use `/config blacklist` to suppress
persistently-spammy sources.

When a ticker is first added (or market news enabled), its currently-existing articles
are marked seen without being posted, so you only get articles published after that —
not a backlog dump.

If a feed fails to fetch 3 times in a row, it backs off (skips being retried every tick)
for a scaling window instead of hammering a broken endpoint forever; a single success
resets it immediately.

## Storage

All state is stored in flat JSON files, no database required. Each is keyed by Discord
server (guild) ID, and each is created automatically the first time something writes to
it — none of them need to exist (or be committed to the repo) for a fresh clone to work,
and all are gitignored since they hold live server/channel IDs:

- `watchlist.json` — `{ "guild_id": ["AAPL", "TSLA"] }`
- `seen.json` — `{ "guild_id": ["https://...", ...] }` (normalized dedup keys)
- `config.json` — `{ "guild_id": { "channel_id": 123456789012345 } }` (ticker news channel)
- `news_config.json` — `{ "guild_id": { "enabled": true, "channel_id": ... } }` (general news channel)
- `permissions.json` — `{ "guild_id": { "allowed_role_id": ..., "command_channels": [id, ...] } }` (delegated role and/or channel restriction; both keys optional/independent)
- `blacklist.json` — `{ "guild_id": ["trefis", ...] }` (URL substrings whose articles are dropped)
- `.env` — secrets (`DISCORD_TOKEN`, optional `ALERT_USER_ID`)

## Running as a service

See `/etc/systemd/system/squawk.service` if deployed on a systemd-based Linux host.
Manage it with:

```
systemctl status squawk
systemctl restart squawk
journalctl -u squawk -f
```

See `CHANGELOG.md` for the reasoning behind notable design decisions.
