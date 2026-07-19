import asyncio
import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import discord
import feedparser
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
_alert_user_id_raw = os.getenv("ALERT_USER_ID")
ALERT_USER_ID = int(_alert_user_id_raw) if _alert_user_id_raw else None

BASE_DIR = Path(__file__).resolve().parent
WATCHLIST_PATH = BASE_DIR / "watchlist.json"
SEEN_PATH = BASE_DIR / "seen.json"
CONFIG_PATH = BASE_DIR / "config.json"
NEWS_CONFIG_PATH = BASE_DIR / "news_config.json"
PERMISSIONS_PATH = BASE_DIR / "permissions.json"
BLACKLIST_PATH = BASE_DIR / "blacklist.json"
VERSION_PATH = BASE_DIR / "VERSION"

_max_tickers_raw = os.getenv("MAX_TICKERS_PER_SERVER")
MAX_TICKERS_PER_SERVER = int(_max_tickers_raw) if _max_tickers_raw else 0

RSS_URL_TEMPLATE = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
QUOTE_CHECK_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
HTTP_USER_AGENT = "Squawk/1.0"


def _fetch_feed_body(url: str) -> bytes:
    """Fetch raw feed bytes with a UA Cloudflare accepts, raising on non-XML responses."""
    req = urllib.request.Request(url, headers={"User-Agent": HTTP_USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        body = resp.read()
    if body[:1] != b"<" or ("html" in ctype and "xml" not in ctype):
        raise ValueError(f"non-feed response (content-type={ctype!r})")
    return body


def _parse_feed(url: str):
    """Fetch via urllib with one retry (PR Newswire's Cloudflare intermittently 301s to a URL that 404s)."""
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            body = _fetch_feed_body(url)
            return feedparser.parse(body)
        except Exception as exc:
            last_exc = exc
    return feedparser.util.FeedParserDict(entries=[], bozo=1, bozo_exception=last_exc)
GENERAL_NEWS_URL = RSS_URL_TEMPLATE.format(ticker="%5EGSPC,%5EDJI,%5EIXIC,%5ERUT")
GENERAL_NEWS_KEY = "__general__"
GENERAL_NEWS_EXCLUDE_PATTERN = re.compile(r"\([A-Z]{1,5}\)|Q[1-4] 20\d\d Earnings Report")
WIRE_FIREHOSES = {
    "GlobeNewswire": "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20from%20Public%20Companies",
}
TICKER_MENTION_RE = re.compile(
    r"\((?:NASDAQ|NYSE|NYSEAMERICAN|AMEX|OTC|OTCMKTS|OTCQB|OTCQX|CBOE):\s*([A-Z0-9.-]+)\)",
    re.I,
)
POLL_INTERVAL_MINUTES = 2
SEEN_CAP = 10000

FEED_FAILURE_BACKOFF_THRESHOLD = 3
FEED_FAILURE_BACKOFF_MAX_MULTIPLIER = 6

WATCHDOG_INTERVAL_MINUTES = 2
WATCHDOG_STALE_MULTIPLIER = 3

WATCHLIST_COOLDOWN_SECONDS = 60.0
WATCHLIST_CHANNEL_COOLDOWN_SECONDS = 15.0
TICKER_RECENT_COOLDOWN_SECONDS = 60.0
MARKET_CHANNEL_COOLDOWN_SECONDS = 15.0
MARKET_RECENT_COOLDOWN_SECONDS = 60.0
STATUS_COOLDOWN_SECONDS = 5.0
CONFIG_COOLDOWN_SECONDS = 15.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("squawk")

PROCESS_START_TIME = datetime.now(timezone.utc)


def load_version() -> str:
    try:
        return VERSION_PATH.read_text(encoding="utf-8").strip() or "dev"
    except OSError:
        return "dev"


VERSION = load_version()


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read %s: %s", path, exc)
        return default


def save_json(path: Path, data):
    """Write via a temp file + atomic rename so a mid-write kill can't corrupt the file."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)


def load_watchlist():
    return load_json(WATCHLIST_PATH, {})


def save_watchlist(watchlist):
    save_json(WATCHLIST_PATH, watchlist)


def load_seen():
    return load_json(SEEN_PATH, {})


def save_seen(seen):
    save_json(SEEN_PATH, seen)


def load_config():
    return load_json(CONFIG_PATH, {})


def save_config(config):
    save_json(CONFIG_PATH, config)


def load_news_config():
    return load_json(NEWS_CONFIG_PATH, {})


def save_news_config(news_config):
    save_json(NEWS_CONFIG_PATH, news_config)


def load_permissions():
    return load_json(PERMISSIONS_PATH, {})


def save_permissions(permissions):
    save_json(PERMISSIONS_PATH, permissions)


def load_blacklist():
    return load_json(BLACKLIST_PATH, {})


def save_blacklist(blacklist):
    save_json(BLACKLIST_PATH, blacklist)


intents = discord.Intents.default()


class SquawkBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.tree = app_commands.CommandTree(self)


bot = SquawkBot()


MARKET_NEWS_LABEL = "Market"

TICKER_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,14}$")


def is_valid_ticker(ticker: str) -> bool:
    return bool(TICKER_PATTERN.match(ticker))


def ticker_exists(ticker: str) -> bool:
    """Return False only if Yahoo definitively reports the symbol doesn't exist (HTTP 404); fail open otherwise."""
    req = urllib.request.Request(
        QUOTE_CHECK_URL.format(ticker=quote(ticker, safe=".-")),
        headers={"User-Agent": HTTP_USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        logger.warning("Ticker existence check for %s returned HTTP %s; allowing", ticker, exc.code)
        return True
    except Exception as exc:
        logger.warning("Ticker existence check for %s failed (%s); allowing", ticker, exc)
        return True


def filter_market_entries(entries):
    """Drop templated single-stock-vs-market blurbs, keeping genuinely broad market news."""
    return [e for e in entries if not GENERAL_NEWS_EXCLUDE_PATTERN.search(e.get("title", ""))]


def extract_tickers(entry) -> set[str]:
    """Pull ticker symbols from '(NASDAQ: AAPL)' style mentions in title and summary."""
    text = (entry.get("title") or "") + " " + (entry.get("summary") or "")
    return {m.upper() for m in TICKER_MENTION_RE.findall(text)}


_TRACKING_PARAM_PREFIXES = ("utm_", "fbclid", "gclid")
_TRACKING_PARAM_EXACT = {".tsrc", "tsrc", "guccounter", "soc_src", "soc_trk"}


def dedup_key(url: str) -> str:
    """Normalize an article URL for de-duplication: lowercase host, drop tracking params."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in _TRACKING_PARAM_EXACT and not k.startswith(_TRACKING_PARAM_PREFIXES)
    ]
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), urlencode(kept), ""))


def is_blacklisted(url: str, patterns: list) -> bool:
    if not patterns:
        return False
    lowered = url.lower()
    return any(p in lowered for p in patterns)


_GNW_SEARCH_URL = "https://www.globenewswire.com/en/search/keyword/{ticker}"
_GNW_ANCHOR_RE = re.compile(
    r'<a\s+href="(/news-release/[^"]+)"[^>]*>\s*([^<]{5,200}?)\s*</a>',
    re.I,
)


def search_globenewswire(ticker: str, limit: int = 5) -> list[tuple[str, str]]:
    """Return up to `limit` (title, url) pairs from GlobeNewswire's per-ticker search page."""
    url = _GNW_SEARCH_URL.format(ticker=quote(ticker, safe=".-"))
    req = urllib.request.Request(url, headers={"User-Agent": HTTP_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("GlobeNewswire search for %s failed: %s", ticker, exc)
        return []
    out: list[tuple[str, str]] = []
    seen_slugs: set[str] = set()
    for m in _GNW_ANCHOR_RE.finditer(body):
        path, title = m.group(1), m.group(2).strip()
        if path in seen_slugs:
            continue
        seen_slugs.add(path)
        out.append((title, f"https://www.globenewswire.com{path}"))
        if len(out) >= limit:
            break
    return out


def format_article(entry, label: str | None = None) -> str:
    title = entry.get("title", "Untitled")
    link = entry.get("link", "")
    article = f"[{title}](<{link}>)"
    return f"**{label}**: {article}" if label else article


feed_failure_counts: dict[str, int] = {}
feed_backoff_until: dict[str, datetime] = {}
feed_last_error: dict[str, str] = {}
last_poll_time: datetime | None = None

seen_lock = asyncio.Lock()


async def merge_seen(new_by_guild: dict[str, list]) -> int:
    """Under the lock: re-read seen.json, append each guild's new URLs (deduped, capped), save.

    Returns the number of genuinely-new URLs added across all guilds.
    """
    if not new_by_guild:
        return 0
    added = 0
    async with seen_lock:
        current = load_seen()
        for gid, urls in new_by_guild.items():
            merged = list(current.get(gid, []))
            merged_set = set(merged)
            for url in urls:
                if url not in merged_set:
                    merged.append(url)
                    merged_set.add(url)
                    added += 1
            if len(merged) > SEEN_CAP:
                merged = merged[-SEEN_CAP:]
            current[gid] = merged
        save_seen(current)
    return added


def _record_feed_failure(ticker: str, message: str) -> bool:
    """Returns True the moment this feed first enters backoff (not on every subsequent failure)."""
    count = feed_failure_counts.get(ticker, 0) + 1
    feed_failure_counts[ticker] = count
    feed_last_error[ticker] = message
    logger.error("Failed to fetch RSS for %s (%d consecutive failure(s)): %s", ticker, count, message)

    just_entered_backoff = False
    if count >= FEED_FAILURE_BACKOFF_THRESHOLD:
        just_entered_backoff = ticker not in feed_backoff_until
        backoff_minutes = min(count, FEED_FAILURE_BACKOFF_MAX_MULTIPLIER) * POLL_INTERVAL_MINUTES
        until = datetime.now(timezone.utc) + timedelta(minutes=backoff_minutes)
        feed_backoff_until[ticker] = until
        logger.warning(
            "Backing off ticker %s for %d minute(s) after %d consecutive failures (next retry at %s)",
            ticker, backoff_minutes, count, until.isoformat(),
        )
    return just_entered_backoff


def _record_feed_success(ticker: str):
    feed_failure_counts.pop(ticker, None)
    feed_backoff_until.pop(ticker, None)
    feed_last_error.pop(ticker, None)


async def send_failure_alerts(key: str):
    """DM the owner of every affected guild, plus ALERT_USER_ID if configured, once per backoff entry."""
    if key == GENERAL_NEWS_KEY:
        label = "market news"
    elif key in WIRE_FIREHOSES:
        label = f"wire feed `{key}`"
    else:
        label = f"ticker `{key}`"
    message = (
        f"Squawk: the feed for {label} has failed {FEED_FAILURE_BACKOFF_THRESHOLD} times in a row "
        f"and is now backing off.\nLast error: {feed_last_error.get(key, 'unknown')}"
    )

    if key == GENERAL_NEWS_KEY:
        news_config = load_news_config()
        guild_ids = [gid for gid, cfg in news_config.items() if cfg.get("enabled") and cfg.get("channel_id")]
    elif key in WIRE_FIREHOSES:
        watchlist = load_watchlist()
        guild_ids = [gid for gid, tickers in watchlist.items() if tickers]
    else:
        watchlist = load_watchlist()
        guild_ids = [gid for gid, tickers in watchlist.items() if key in tickers]

    notified_owners = set()
    for guild_id in guild_ids:
        guild = bot.get_guild(int(guild_id))
        if guild is None or guild.owner_id is None or guild.owner_id in notified_owners:
            continue
        try:
            owner = guild.owner or await bot.fetch_user(guild.owner_id)
            await owner.send(message)
            notified_owners.add(guild.owner_id)
        except discord.DiscordException as exc:
            logger.error("Failed to DM owner of guild %s: %s", guild_id, exc)

    if ALERT_USER_ID:
        try:
            alert_user = await bot.fetch_user(ALERT_USER_ID)
            await alert_user.send(message)
        except discord.DiscordException as exc:
            logger.error("Failed to DM alert user %s: %s", ALERT_USER_ID, exc)


async def _fetch_feed(cache: dict, key: str, url: str):
    """Fetch and cache a feed under `key`, applying failure backoff/tracking."""
    now = datetime.now(timezone.utc)
    backoff_until = feed_backoff_until.get(key)
    if backoff_until and now < backoff_until:
        return

    try:
        feed = await asyncio.to_thread(_parse_feed,url)
    except Exception as exc:
        if _record_feed_failure(key, str(exc)):
            await send_failure_alerts(key)
        return

    if getattr(feed, "bozo", False) and not getattr(feed, "entries", None):
        if _record_feed_failure(key, str(getattr(feed, "bozo_exception", "unknown error"))):
            await send_failure_alerts(key)
        return

    _record_feed_success(key)
    cache[key] = feed


def _post_new_entries(feed, guild_seen_set: set, new_urls: list, label: str | None = None, blacklist: list | None = None) -> list:
    """Return (content, entry, url) for entries not already seen or blacklisted, recording them as seen.

    De-dup keys stored in guild_seen_set/new_urls are normalized URLs (dedup_key), so the
    same article arriving with different tracking params isn't re-posted. Blacklisted URLs
    are skipped without being marked seen, so un-blacklisting can surface them again.
    """
    to_send = []
    for entry in feed.entries:
        article_url = entry.get("link", "")
        if not article_url or is_blacklisted(article_url, blacklist):
            continue
        key = dedup_key(article_url)
        if key in guild_seen_set:
            continue
        to_send.append((format_article(entry, label), entry, article_url))
        guild_seen_set.add(key)
        new_urls.append(key)
    return to_send


@tasks.loop(minutes=POLL_INTERVAL_MINUTES)
async def poll_feeds():
    global last_poll_time

    watchlist = load_watchlist()
    news_config = load_news_config()
    if not watchlist and not news_config:
        last_poll_time = datetime.now(timezone.utc)
        return

    config = load_config()
    blacklist_cfg = load_blacklist()
    seen = load_seen()

    all_tickers = set()
    for tickers in watchlist.values():
        all_tickers.update(tickers)

    feeds_cache = {}
    for ticker in all_tickers:
        await _fetch_feed(feeds_cache, ticker, RSS_URL_TEMPLATE.format(ticker=ticker))

    if all_tickers:
        for name, url in WIRE_FIREHOSES.items():
            await _fetch_feed(feeds_cache, name, url)

    if any(g.get("enabled") and g.get("channel_id") for g in news_config.values()):
        await _fetch_feed(feeds_cache, GENERAL_NEWS_KEY, GENERAL_NEWS_URL)

    wire_entries: list[tuple[object, set[str]]] = []
    for name in WIRE_FIREHOSES:
        feed = feeds_cache.get(name)
        if feed is None:
            continue
        for entry in feed.entries:
            tickers_in_entry = extract_tickers(entry)
            if tickers_in_entry:
                wire_entries.append((entry, tickers_in_entry))

    new_by_guild: dict[str, list] = {}

    for guild_id, tickers in watchlist.items():
        if not tickers:
            continue

        guild_config = config.get(guild_id)
        channel_id = guild_config.get("channel_id") if guild_config else None
        if not channel_id:
            continue

        channel = bot.get_channel(int(channel_id))
        if channel is None:
            logger.error("Configured channel %s for guild %s not found or inaccessible", channel_id, guild_id)
            continue

        guild_seen_list = seen.get(guild_id, [])
        guild_seen_set = set(guild_seen_list)
        guild_blacklist = blacklist_cfg.get(guild_id, [])
        new_urls = []

        for ticker in tickers:
            feed = feeds_cache.get(ticker)
            if feed is None:
                continue

            for content, entry, article_url in _post_new_entries(feed, guild_seen_set, new_urls, label=ticker, blacklist=guild_blacklist):
                try:
                    await channel.send(content)
                    logger.info("Posted article for %s in guild %s: %s", ticker, guild_id, entry.get("title", "Untitled"))
                except discord.DiscordException as exc:
                    logger.error("Failed to post article for %s in guild %s: %s", ticker, guild_id, exc)

        watchset = set(tickers)
        for entry, entry_tickers in wire_entries:
            matched = watchset & entry_tickers
            if not matched:
                continue
            article_url = entry.get("link", "")
            if not article_url or is_blacklisted(article_url, guild_blacklist):
                continue
            key = dedup_key(article_url)
            if key in guild_seen_set:
                continue
            label = ", ".join(sorted(matched))
            try:
                await channel.send(format_article(entry, label))
                logger.info("Posted wire article for %s in guild %s: %s", label, guild_id, entry.get("title", "Untitled"))
            except discord.DiscordException as exc:
                logger.error("Failed to post wire article for guild %s: %s", guild_id, exc)
                continue
            guild_seen_set.add(key)
            new_urls.append(key)

        if new_urls:
            new_by_guild.setdefault(guild_id, []).extend(new_urls)

    general_feed = feeds_cache.get(GENERAL_NEWS_KEY)
    if general_feed is not None:
        general_feed.entries = filter_market_entries(general_feed.entries)
        for guild_id, guild_news_config in news_config.items():
            channel_id = guild_news_config.get("channel_id")
            if not guild_news_config.get("enabled") or not channel_id:
                continue

            channel = bot.get_channel(int(channel_id))
            if channel is None:
                logger.error("Configured news channel %s for guild %s not found or inaccessible", channel_id, guild_id)
                continue

            guild_seen_set = set(seen.get(guild_id, [])) | set(new_by_guild.get(guild_id, []))
            guild_blacklist = blacklist_cfg.get(guild_id, [])
            new_urls = []

            for content, entry, article_url in _post_new_entries(general_feed, guild_seen_set, new_urls, label=MARKET_NEWS_LABEL, blacklist=guild_blacklist):
                try:
                    await channel.send(content)
                    logger.info("Posted general news article in guild %s: %s", guild_id, entry.get("title", "Untitled"))
                except discord.DiscordException as exc:
                    logger.error("Failed to post general news article in guild %s: %s", guild_id, exc)

            if new_urls:
                new_by_guild.setdefault(guild_id, []).extend(new_urls)

    await merge_seen(new_by_guild)

    last_poll_time = datetime.now(timezone.utc)


@poll_feeds.before_loop
async def before_poll_feeds():
    await bot.wait_until_ready()


@tasks.loop(minutes=WATCHDOG_INTERVAL_MINUTES)
async def watchdog():
    """Force-exit if the poll loop appears hung, so systemd's Restart=on-failure recovers it.

    Crashes are already handled by systemd; this covers the case where the process is
    alive but poll_feeds silently stopped completing (e.g. stuck on a hanging network call).
    """
    stale_after = timedelta(minutes=POLL_INTERVAL_MINUTES * WATCHDOG_STALE_MULTIPLIER)
    now = datetime.now(timezone.utc)

    if now - PROCESS_START_TIME < stale_after:
        return

    if last_poll_time is None or (now - last_poll_time) > stale_after:
        logger.critical(
            "Poll loop appears hung (last successful poll: %s) - exiting for systemd to restart",
            last_poll_time,
        )
        os._exit(1)


@watchdog.before_loop
async def before_watchdog():
    await bot.wait_until_ready()


def rate_limited(seconds: float):
    def _key(interaction: discord.Interaction):
        if isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild:
            return None
        return app_commands.Cooldown(1, seconds)
    return app_commands.checks.dynamic_cooldown(_key)


async def check_authorized(interaction: discord.Interaction) -> bool:
    member = interaction.user
    if not isinstance(member, discord.Member):
        return False
    return member.guild_permissions.manage_guild


async def check_channel_allowed(interaction: discord.Interaction) -> bool:
    member = interaction.user
    if isinstance(member, discord.Member) and member.guild_permissions.manage_guild:
        return True
    guild_perms = load_permissions().get(str(interaction.guild_id), {})
    mode = guild_perms.get("channel_mode", "all")
    exceptions = guild_perms.get("channel_exceptions") or []
    if mode == "all":
        return interaction.channel_id not in exceptions
    return interaction.channel_id in exceptions


def authorized():
    return app_commands.check(check_authorized)


def channel_allowed():
    return app_commands.check(check_channel_allowed)


async def handle_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(
            f"Slow down - try again in {error.retry_after:.1f}s.", ephemeral=True
        )
        return
    if isinstance(error, (app_commands.MissingPermissions, app_commands.CheckFailure)):
        await interaction.response.send_message(
            "You don't have permission to use this command here.",
            ephemeral=True,
        )
        return
    logger.error("Unhandled app command error: %s", error)
    if not interaction.response.is_done():
        await interaction.response.send_message("Something went wrong running that command.", ephemeral=True)


watchlist_group = app_commands.Group(
    name="watchlist",
    description="Manage this server's tracked ticker watchlist",
    guild_only=True,
)


def parse_ticker_input(raw: str) -> list[str]:
    """Split a comma-separated ticker argument into a deduped, uppercased list (order-preserving)."""
    out = []
    for part in raw.split(","):
        t = part.strip().upper()
        if t and t not in out:
            out.append(t)
    return out


@watchlist_group.command(name="ticker", description="Add or remove tickers (comma-separate for multiple: AAPL,MSFT)")
@app_commands.describe(action="Add or remove", ticker="Ticker(s), comma-separated for multiple, e.g. AAPL,MSFT,BRK-B")
@app_commands.choices(action=[
    app_commands.Choice(name="add", value="add"),
    app_commands.Choice(name="remove", value="remove"),
])
@authorized()
async def watchlist_ticker(interaction: discord.Interaction, action: app_commands.Choice[str], ticker: str):
    requested = parse_ticker_input(ticker)
    if not requested:
        await interaction.response.send_message("Please provide at least one ticker symbol.", ephemeral=True)
        return
    guild_id = str(interaction.guild_id)
    watchlist = load_watchlist()
    guild_list = watchlist.get(guild_id, [])

    if action.value == "add":
        invalid = [t for t in requested if not is_valid_ticker(t)]
        already = [t for t in requested if t in guild_list and is_valid_ticker(t)]
        candidates = [t for t in requested if is_valid_ticker(t) and t not in guild_list]

        if MAX_TICKERS_PER_SERVER and len(guild_list) + len(candidates) > MAX_TICKERS_PER_SERVER:
            remaining = max(0, MAX_TICKERS_PER_SERVER - len(guild_list))
            await interaction.response.send_message(
                f"This server can track at most {MAX_TICKERS_PER_SERVER} tickers "
                f"({len(guild_list)} currently tracked, {remaining} slots left).",
                ephemeral=True,
            )
            return

        if not candidates:
            parts = []
            if already:
                parts.append(f"already tracked: {', '.join(f'`{t}`' for t in already)}")
            if invalid:
                parts.append(f"invalid: {', '.join(f'`{t}`' for t in invalid)}")
            await interaction.response.send_message(
                "Nothing added - " + ("; ".join(parts) if parts else "no valid tickers given") + ".",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        to_add = []
        nonexistent = []
        for t in candidates:
            if await asyncio.to_thread(ticker_exists, t):
                to_add.append(t)
            else:
                nonexistent.append(t)

        if not to_add:
            parts = []
            if nonexistent:
                parts.append(f"not found on Yahoo Finance: {', '.join(f'`{t}`' for t in nonexistent)}")
            if already:
                parts.append(f"already tracked: {', '.join(f'`{t}`' for t in already)}")
            if invalid:
                parts.append(f"invalid: {', '.join(f'`{t}`' for t in invalid)}")
            await interaction.followup.send("Nothing added - " + "; ".join(parts) + ".")
            return

        guild_list.extend(to_add)
        watchlist[guild_id] = guild_list
        save_watchlist(watchlist)

        seeded_keys: dict[str, list] = {guild_id: []}
        for t in to_add:
            try:
                feed = await asyncio.to_thread(_parse_feed,RSS_URL_TEMPLATE.format(ticker=t))
                seeded_keys[guild_id].extend(dedup_key(e["link"]) for e in feed.entries if e.get("link"))
            except Exception as exc:
                logger.error("Failed to seed seen articles for %s in guild %s: %s", t, guild_id, exc)
        seeded_count = await merge_seen(seeded_keys)

        logger.info("Watchlist modified for guild %s: added %s (seeded %d articles)", guild_id, ", ".join(to_add), seeded_count)

        lines = [f"Added {', '.join(f'`{t}`' for t in to_add)} to the watchlist. Only new articles from now on will be posted."]
        if nonexistent:
            lines.append(f"Not found on Yahoo Finance (skipped): {', '.join(f'`{t}`' for t in nonexistent)}")
        if already:
            lines.append(f"Already tracked (skipped): {', '.join(f'`{t}`' for t in already)}")
        if invalid:
            lines.append(f"Invalid (skipped): {', '.join(f'`{t}`' for t in invalid)}")
        await interaction.followup.send("\n".join(lines))
    else:
        removed = [t for t in requested if t in guild_list]
        not_found = [t for t in requested if t not in guild_list]

        if not removed:
            await interaction.response.send_message(
                f"None of those are on the watchlist: {', '.join(f'`{t}`' for t in not_found)}.", ephemeral=True
            )
            return

        for t in removed:
            guild_list.remove(t)
        watchlist[guild_id] = guild_list
        save_watchlist(watchlist)
        logger.info("Watchlist modified for guild %s: removed %s", guild_id, ", ".join(removed))

        lines = [f"Removed {', '.join(f'`{t}`' for t in removed)} from the watchlist."]
        if not_found:
            lines.append(f"Not on the watchlist (skipped): {', '.join(f'`{t}`' for t in not_found)}")
        await interaction.response.send_message("\n".join(lines))


@watchlist_group.command(name="show", description="Show this server's tracked tickers")
@rate_limited(WATCHLIST_COOLDOWN_SECONDS)
@channel_allowed()
async def watchlist_show(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    watchlist = load_watchlist()
    guild_list = watchlist.get(guild_id, [])

    if not guild_list:
        await interaction.response.send_message("This server's watchlist is empty.")
        return

    tickers = ", ".join(f"`{t}`" for t in guild_list)
    await interaction.response.send_message(f"Tracked tickers: {tickers}")


@watchlist_group.command(name="channel", description="Set or clear the channel news articles are posted to for this server")
@app_commands.describe(action="Set or clear the news channel", channel="Channel to post news articles to (required for set)")
@app_commands.choices(action=[
    app_commands.Choice(name="set", value="set"),
    app_commands.Choice(name="clear", value="clear"),
])
@authorized()
async def watchlist_channel(
    interaction: discord.Interaction,
    action: app_commands.Choice[str],
    channel: discord.TextChannel = None,
):
    guild_id = str(interaction.guild_id)
    config = load_config()

    if action.value == "set":
        if channel is None:
            await interaction.response.send_message("You must specify a channel when using `set`.", ephemeral=True)
            return
        config[guild_id] = {"channel_id": channel.id}
        save_config(config)
        logger.info("News channel configured for guild %s: #%s (%s)", guild_id, channel.name, channel.id)
        await interaction.response.send_message(f"News articles will now be posted to {channel.mention}.")
    else:
        if guild_id in config:
            del config[guild_id]
            save_config(config)
        logger.info("News channel cleared for guild %s", guild_id)
        await interaction.response.send_message("News channel cleared. This server will no longer receive automatic posts.")


RECENT_ARTICLE_COUNT = 3


@watchlist_group.error
async def watchlist_group_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    await handle_command_error(interaction, error)


config_group = app_commands.Group(
    name="config",
    description="Server-wide Squawk configuration (permissions, channel restrictions, blacklist)",
    guild_only=True,
)


@config_group.command(name="show", description="Show this server's current Squawk configuration")
@app_commands.checks.has_permissions(manage_guild=True)
async def config_show(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    cfg = load_config()
    news_cfg = load_news_config()
    perms = load_permissions()
    bl = load_blacklist()

    guild_cfg = cfg.get(guild_id, {})
    guild_news = news_cfg.get(guild_id, {})
    guild_perms = perms.get(guild_id, {})
    guild_bl = bl.get(guild_id, [])

    ticker_ch = guild_cfg.get("channel_id")
    market_ch = guild_news.get("channel_id") if guild_news.get("enabled") else None
    channel_mode = guild_perms.get("channel_mode", "all")
    exceptions = guild_perms.get("channel_exceptions", [])

    if exceptions:
        exc_label = "blocked in" if channel_mode == "all" else "allowed only in"
        ch_text = f"**{channel_mode}** - {exc_label} {', '.join(f'<#{c}>' for c in exceptions)}"
    else:
        ch_text = f"**{channel_mode}** (no exceptions)"

    lines = [
        f"**Ticker news channel:** {f'<#{ticker_ch}>' if ticker_ch else 'not set'}",
        f"**Market news channel:** {f'<#{market_ch}>' if market_ch else 'not set'}",
        f"**Read-only command access:** {ch_text}",
        f"**Article blacklist:** {', '.join(f'`{p}`' for p in guild_bl) if guild_bl else 'none'}",
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


_CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>|(\d{15,20})")


def parse_channel_mentions(raw: str, guild: discord.Guild) -> tuple[list[int], list[str]]:
    ids: list[int] = []
    invalid: list[str] = []
    for token in raw.replace(",", " ").split():
        m = _CHANNEL_MENTION_RE.fullmatch(token.strip())
        if not m:
            invalid.append(token)
            continue
        cid = int(m.group(1) or m.group(2))
        if guild.get_channel(cid) is None:
            invalid.append(token)
            continue
        if cid not in ids:
            ids.append(cid)
    return ids, invalid


@config_group.command(
    name="channel",
    description="Set where regular users can use Squawk's read-only commands (Manage Server is always unrestricted)",
)
@app_commands.describe(
    mode="'all' = allowed everywhere, 'none' = blocked everywhere. Exceptions flip the rule for listed channels.",
    exceptions="Space or comma-separated #channel mentions. Empty string clears the list.",
)
@app_commands.choices(mode=[
    app_commands.Choice(name="all", value="all"),
    app_commands.Choice(name="none", value="none"),
])
@app_commands.checks.has_permissions(manage_guild=True)
async def config_channel(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str] = None,
    exceptions: str = None,
):
    if mode is None and exceptions is None:
        await interaction.response.send_message(
            "Pass `mode` to change the default, `exceptions` to set the exception list, or both.",
            ephemeral=True,
        )
        return

    guild_id = str(interaction.guild_id)
    permissions = load_permissions()
    guild_perms = permissions.get(guild_id, {})
    reply_parts: list[str] = []

    if mode is not None:
        guild_perms["channel_mode"] = mode.value
        logger.info("Channel mode set to %s for guild %s", mode.value, guild_id)
        reply_parts.append(
            f"Mode: **{mode.value}** - read-only commands are "
            f"{'allowed' if mode.value == 'all' else 'blocked'} everywhere by default."
        )

    if exceptions is not None:
        if not exceptions.strip():
            guild_perms["channel_exceptions"] = []
            reply_parts.append("Exceptions cleared.")
        else:
            ids, invalid = parse_channel_mentions(exceptions, interaction.guild)
            guild_perms["channel_exceptions"] = ids
            if invalid:
                reply_parts.append(f"Skipped invalid entries: {', '.join(f'`{x}`' for x in invalid)}")
            current_mode = guild_perms.get("channel_mode", "all")
            action_word = "Blocked" if current_mode == "all" else "Allowed only in"
            if ids:
                reply_parts.append(f"{action_word}: {', '.join(f'<#{c}>' for c in ids)}")
            else:
                reply_parts.append("Exceptions cleared (no valid channels given).")

    permissions[guild_id] = guild_perms
    save_permissions(permissions)
    await interaction.response.send_message("\n".join(reply_parts))


@config_group.command(name="blacklist", description="Skip articles whose link URL contains this text (e.g. 'trefis' to drop that publisher)")
@app_commands.describe(
    action="Add or remove a pattern, show the current list, or clear it entirely",
    pattern="Text that must appear in the article's URL for it to be skipped (required for add/remove)",
)
@app_commands.choices(action=[
    app_commands.Choice(name="add", value="add"),
    app_commands.Choice(name="remove", value="remove"),
    app_commands.Choice(name="show", value="show"),
    app_commands.Choice(name="clear", value="clear"),
])
@app_commands.checks.has_permissions(manage_guild=True)
async def config_blacklist(
    interaction: discord.Interaction,
    action: app_commands.Choice[str],
    pattern: str = None,
):
    guild_id = str(interaction.guild_id)
    blacklist = load_blacklist()
    patterns = blacklist.get(guild_id, [])

    if action.value in ("add", "remove"):
        cleaned = (pattern or "").strip().lower()
        if not cleaned:
            await interaction.response.send_message(f"You must specify a pattern when using `{action.value}`.", ephemeral=True)
            return

        if action.value == "add":
            if cleaned in patterns:
                await interaction.response.send_message(f"`{cleaned}` is already blacklisted.", ephemeral=True)
                return
            patterns.append(cleaned)
            blacklist[guild_id] = patterns
            save_blacklist(blacklist)
            logger.info("Blacklist: added %r for guild %s", cleaned, guild_id)
            await interaction.response.send_message(
                f"Blacklisted `{cleaned}` - articles whose link contains it won't be posted."
            )
        else:
            if cleaned not in patterns:
                await interaction.response.send_message(f"`{cleaned}` isn't in the blacklist.", ephemeral=True)
                return
            patterns.remove(cleaned)
            blacklist[guild_id] = patterns
            save_blacklist(blacklist)
            logger.info("Blacklist: removed %r for guild %s", cleaned, guild_id)
            await interaction.response.send_message(f"Removed `{cleaned}` from the blacklist.")
    elif action.value == "clear":
        blacklist[guild_id] = []
        save_blacklist(blacklist)
        logger.info("Blacklist cleared for guild %s", guild_id)
        await interaction.response.send_message("Blacklist cleared.")
    else:
        if not patterns:
            await interaction.response.send_message("The blacklist is empty.")
        else:
            await interaction.response.send_message(
                "Blacklisted patterns: " + ", ".join(f"`{p}`" for p in patterns)
            )


@config_group.error
async def config_group_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    await handle_command_error(interaction, error)


ticker_group = app_commands.Group(
    name="ticker",
    description="Look up news for any ticker, independent of this server's watchlist",
    guild_only=True,
)


@ticker_group.command(name="recent", description="Show the 3 most recent articles for a ticker")
@app_commands.describe(ticker="Ticker symbol, e.g. AAPL")
@rate_limited(TICKER_RECENT_COOLDOWN_SECONDS)
@channel_allowed()
async def ticker_recent(interaction: discord.Interaction, ticker: str):
    ticker = ticker.strip().upper()
    if not is_valid_ticker(ticker):
        await interaction.response.send_message(
            f"`{ticker}` doesn't look like a valid ticker symbol. Use letters, digits, "
            "`.`, or `-` (e.g. `AAPL`, `BRK-B`, `AVIO.MI`, `BTC-USD`).",
            ephemeral=True,
        )
        return
    await interaction.response.defer()

    guild_blacklist = load_blacklist().get(str(interaction.guild_id), [])

    collected: list[tuple[str, str]] = []
    seen_keys: set[str] = set()

    yahoo_url = RSS_URL_TEMPLATE.format(ticker=ticker)
    try:
        feed = await asyncio.to_thread(_parse_feed,yahoo_url)
    except Exception as exc:
        logger.warning("Yahoo RSS for %s failed: %s", ticker, exc)
        feed = None
    if feed is not None:
        for e in feed.entries:
            link = e.get("link", "")
            title = e.get("title", "").strip()
            if not link or not title or is_blacklisted(link, guild_blacklist):
                continue
            key = dedup_key(link)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            collected.append((title, link))

    for title, link in await asyncio.to_thread(search_globenewswire, ticker, 5):
        if is_blacklisted(link, guild_blacklist):
            continue
        key = dedup_key(link)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        collected.append((title, link))

    if not collected:
        await interaction.followup.send(f"No recent articles found for `{ticker}`.")
        return

    lines = [f"**Recent news for `{ticker}`**"]
    for title, link in collected[:RECENT_ARTICLE_COUNT]:
        lines.append(f"[{title}](<{link}>)")

    await interaction.followup.send("\n".join(lines))


@ticker_group.error
async def ticker_group_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    await handle_command_error(interaction, error)


news_group = app_commands.Group(
    name="market",
    description="Manage this server's market news feed (not tied to a ticker)",
    guild_only=True,
)


@news_group.command(name="channel", description="Set or clear the channel market news is posted to")
@app_commands.describe(action="Set or clear the news channel", channel="Channel to post market news to (required for set)")
@app_commands.choices(action=[
    app_commands.Choice(name="set", value="set"),
    app_commands.Choice(name="clear", value="clear"),
])
@authorized()
async def news_channel(
    interaction: discord.Interaction,
    action: app_commands.Choice[str],
    channel: discord.TextChannel = None,
):
    guild_id = str(interaction.guild_id)
    news_config = load_news_config()

    if action.value == "set":
        if channel is None:
            await interaction.response.send_message("You must specify a channel when using `set`.", ephemeral=True)
            return

        await interaction.response.defer()

        news_config[guild_id] = {"enabled": True, "channel_id": channel.id}
        save_news_config(news_config)

        seeded_count = 0
        try:
            feed = await asyncio.to_thread(_parse_feed,GENERAL_NEWS_URL)
            existing_keys = [dedup_key(entry["link"]) for entry in feed.entries if entry.get("link")]
            if existing_keys:
                seeded_count = await merge_seen({guild_id: existing_keys})
        except Exception as exc:
            logger.error("Failed to seed seen market news articles for guild %s: %s", guild_id, exc)

        logger.info(
            "Market news channel configured for guild %s: #%s (%s), seeded %d existing articles",
            guild_id, channel.name, channel.id, seeded_count,
        )
        await interaction.followup.send(
            f"Market news will now be posted to {channel.mention}. Only new articles published from now on will be posted."
        )
    else:
        if guild_id in news_config:
            del news_config[guild_id]
            save_news_config(news_config)
        logger.info("Market news disabled for guild %s", guild_id)
        await interaction.response.send_message("Market news disabled for this server.")


@news_group.command(name="recent", description="Show the 3 most recent market news articles")
@rate_limited(MARKET_RECENT_COOLDOWN_SECONDS)
@channel_allowed()
async def news_recent(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        feed = await asyncio.to_thread(_parse_feed,GENERAL_NEWS_URL)
    except Exception as exc:
        logger.error("Failed to fetch market news feed: %s", exc)
        await interaction.followup.send("Failed to fetch market news.")
        return

    if getattr(feed, "bozo", False) and not getattr(feed, "entries", None):
        logger.error("Malformed market news feed: %s", getattr(feed, "bozo_exception", "unknown error"))
        await interaction.followup.send("No market news articles found.")
        return

    guild_blacklist = load_blacklist().get(str(interaction.guild_id), [])
    entries = [
        e for e in filter_market_entries(feed.entries)
        if not is_blacklisted(e.get("link", ""), guild_blacklist)
    ][:RECENT_ARTICLE_COUNT]
    if not entries:
        await interaction.followup.send("No recent market news articles found.")
        return

    lines = ["**Recent market news**"]
    for entry in entries:
        title = entry.get("title", "Untitled")
        link = entry.get("link", "")
        lines.append(f"[{title}](<{link}>)")

    await interaction.followup.send("\n".join(lines))


@news_group.error
async def news_group_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    await handle_command_error(interaction, error)


bot.tree.add_command(watchlist_group)
bot.tree.add_command(ticker_group)
bot.tree.add_command(news_group)
bot.tree.add_command(config_group)


@bot.tree.command(name="squawk", description="Show this server's Squawk configuration and status")
@app_commands.guild_only()
@rate_limited(STATUS_COOLDOWN_SECONDS)
@channel_allowed()
async def squawk_status(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    tickers = load_watchlist().get(guild_id, [])

    poll_text = f"<t:{int(last_poll_time.timestamp())}:R>" if last_poll_time else "pending"

    lines = [
        f"[{VERSION}](<https://github.com/yerettegroup/squawk-bot>)",
        f"Uptime: <t:{int(PROCESS_START_TIME.timestamp())}:R>",
        f"Tickers tracked: {len(tickers)}",
        f"Last poll: {poll_text}",
    ]

    failing = [t for t in tickers if t in feed_backoff_until]
    failing.extend(name for name in WIRE_FIREHOSES if name in feed_backoff_until)
    if GENERAL_NEWS_KEY in feed_backoff_until:
        failing.append("market news")
    if failing:
        issues = ", ".join(f"`{t}`" for t in failing)
        lines.append(f"Feeds in backoff: {issues}")

    await interaction.response.send_message("\n".join(lines))


@squawk_status.error
async def squawk_status_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    await handle_command_error(interaction, error)


_ready_once = False


@bot.event
async def on_ready():
    global _ready_once
    logger.info("Squawk bot logged in as %s (id: %s)", bot.user, bot.user.id)

    if not _ready_once:
        for guild in bot.guilds:
            try:
                bot.tree.clear_commands(guild=guild)
                await bot.tree.sync(guild=guild)
            except discord.DiscordException as exc:
                logger.error("Failed to clear guild-scoped commands for %s: %s", guild.id, exc)

        try:
            synced = await bot.tree.sync()
            logger.info("Synced %d slash command(s)", len(synced))
        except discord.DiscordException as exc:
            logger.error("Failed to sync slash commands: %s", exc)

        _ready_once = True

    if not poll_feeds.is_running():
        poll_feeds.start()
        logger.info("Started RSS poll loop (interval: %d minutes)", POLL_INTERVAL_MINUTES)

    if not watchdog.is_running():
        watchdog.start()
        logger.info("Started poll-loop watchdog (interval: %d minutes)", WATCHDOG_INTERVAL_MINUTES)


def main():
    if not DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")

    logger.info("Starting Squawk bot...")
    bot.run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
