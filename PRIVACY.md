# Squawk Privacy Policy

_Last updated: 2026-07-11_

This policy describes what data the Squawk Discord bot ("the Bot") collects, why, and how it is handled. It applies to the hosted instance operated by Yerette Group and to any self-hosted deployment of the source code at [github.com/yerettegroup/squawk-bot](https://github.com/yerettegroup/squawk-bot).

## What we store

The Bot stores only the minimum data needed to function. Everything is scoped to the Discord server (guild) it was set in.

- **Server ID** - the numeric Discord identifier of servers the Bot is a member of.
- **Channel IDs** - the channels an admin has designated to receive news posts and, optionally, the channels where non-admin users may run read-only commands.
- **Ticker symbols** - the list of stock ticker symbols each server has added to its watchlist.
- **Seen article URLs** - a rolling list (capped at 1000 per server) of URLs the Bot has already posted, used to avoid duplicates.
- **URL blacklist patterns** - any URL substrings an admin has configured to filter out spammy sources.

## What we do NOT store

- Message content, chat history, or any messages sent in your server.
- User IDs, usernames, or any personal information about server members (other than the Discord-supplied user ID of the person invoking a command, used only in-memory for the duration of that command).
- Payment information. The hosted instance is free.
- Analytics, telemetry, or tracking cookies.

## How data is stored

All server-scoped state is written to flat JSON files on the host operating the Bot. No third-party database or analytics service is used. On the hosted instance, files live on a private VPS accessible only to the operator.

## Third-party services

The Bot fetches RSS feeds from Yahoo Finance (`feeds.finance.yahoo.com`) and checks ticker validity against Yahoo's public quote endpoint. No server-identifying information is sent to Yahoo - the Bot only requests public feeds by ticker symbol. Yahoo's own privacy policy applies to those requests.

## Data retention and deletion

If the Bot is removed from your server, its stored data for that server is retained on the host until manually purged. To request deletion of your server's data from the hosted instance, email hello@yerettegroup.com with your server ID.

For self-hosted deployments, the operator of that instance is the data controller and this policy does not apply to them - review the operator's own policies.

## Contact

For privacy, data-deletion, legal, or other policy questions, email **hello@yerettegroup.com**. For bot bugs, feature requests, or anything code-related, open an issue at [github.com/yerettegroup/squawk-bot/issues](https://github.com/yerettegroup/squawk-bot/issues).

## Changes

This policy may be updated. Material changes will be reflected in the "Last updated" date at the top of this document. The version history is available in the repository's Git log.
