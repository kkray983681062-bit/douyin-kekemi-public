# Kekemi · Douyin Live Dashboard

A self-hosted dashboard for monitoring multiple Douyin livestream rooms, reading live chat, viewing available audience and co-host rosters, and reviewing gift analytics and leaderboards.

[中文说明](README.md) · [Features](docs/FEATURES.md) · [Deployment](docs/DEPLOYMENT.md) · [Privacy](docs/PRIVACY.md)

## Features

- **Multiple rooms:** resolve Douyin handles, profile URLs and livestream URLs; configure persistent rooms and reconnect after a new session starts.
- **Audience and co-hosts:** current rosters, available mic-seat information and ticket totals with separate session and daily scopes.
- **Anonymous-user records:** match available identifiers against recorded identities; retain anonymous status when evidence is insufficient.
- **Chat and gifts:** room-isolated feeds, deduplication, resolved gift recipients, scroll freeze and a new-message indicator.
- **Leaderboards:** daily and weekly visitor spending, host receipts, period selection and supported historical archives.
- **Gift management:** catalog search, price sources, name / skin price overrides, change history and recipient corrections.
- **Text-inactivity alerts:** per-room subscriptions, unread counts, browser notifications and chat snapshots.
- **Account controls:** invitation-based registration, approval, login, password recovery and member / admin / super-admin roles.
- **Operations:** SQLite storage, Supabase-backed authentication, gzip, ETag / 304, response caching and Docker / Railway configuration.

Text-inactivity alerts use captured chat messages. They do **not** listen to microphone audio. The default threshold is 15 minutes, with counts grouped into two-hour periods. Ordinary chat refreshes incrementally about every three seconds; selected events use SSE.

## Run locally

Use Python 3.10+ and Node.js 20+. Clone the repository, create a virtual environment, copy `.env.example` to `.env`, and provide your own authorized Douyin cookies locally.

```bash
pip install -r requirements.txt
npm ci
python web_listener.py
```

Open [localhost:5000](http://127.0.0.1:5000). Local development can run without authentication on loopback. Production requires authentication and server-side configuration; see [deployment instructions](docs/DEPLOYMENT.md).

## Data boundaries

Gift totals cover captured events with known quantities and prices, not complete platform history or settlement revenue. Unknown values remain unknown. Audience data depends on the upstream interface. Chat and gift details normally expire after seven days; identity records and certain archives follow different retention rules.

This distribution uses synthetic examples and excludes private deployment records, credentials and production data. Keep personal configuration and exports out of Git.

Keywords: Douyin livestream monitoring, live chat, danmaku, audience roster, co-host management, gift analytics, leaderboards, anonymous users, Python, Flask, SQLite, WebSocket, SSE, Supabase, Railway.
