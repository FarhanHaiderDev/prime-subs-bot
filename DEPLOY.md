# Deploying the bot

The bot is one long-polling process plus a tiny health server. State lives in MongoDB (Atlas);
nothing is stored on the container's disk.

## Requirements

- A MongoDB database (Atlas or self-hosted) reachable from the bot.
- A Telegram bot token, and the numeric Telegram user id of the admin.
- A Pandora Digital reseller API key.
- **Exactly one running instance.** Two bots polling the same token conflict, and two instances
  could race on an order. Do not enable rolling/overlapping deploys.

## Environment variables

Set these in the container's environment (`.env` for docker compose, or your host's secret
store). Names only; values are never documented or committed. Copy `.env.example` as a start.

| Name | Required | Purpose |
|---|---|---|
| `BOT_TOKEN` | yes | Telegram bot token |
| `ADMIN_CHAT_ID` | yes | Telegram **user** id of the admin (approves orders). Not a group id |
| `SUPPLIER_2_API_KEY` | yes | Pandora Digital API key (the live supplier) |
| `MONGODB_URI` | yes | MongoDB connection string |
| `MONGODB_DB` | no (default `prime_subs_bot`) | Database name |
| `ENCRYPTION_KEY` | yes | Fernet key that encrypts stored credentials. Back it up separately (see below) |
| `BINANCE_PAY_ID` | at least one payment method | Binance Pay ID/email shown to customers |
| `BEP20_ADDRESS` | at least one payment method | BEP20 USDT address shown to customers |
| `TRC20_ADDRESS` | at least one payment method | TRC20 USDT address shown to customers |
| `MARKUP_PERCENT` | no (default `40`) | Percentage added to the supplier cost |
| `MARKUP_FIXED` | no (default `0`) | Fixed amount added after the percentage |
| `SUPPORT_USERNAME` | no | Support contact shown in the Support screen |
| `SUPPLIER_API_KEY` | no | Canboso key. Unused while `CANBOSO_ENABLED` is off |
| `CANBOSO_ENABLED` | no (default off) | Keep unset/`false`: the bot refuses to start with it on while Canboso purchases are not implemented |
| `PORT` | no (default `8080`) | Health server port |

Test-only (never set these in production): `MONGODB_TEST_URI`, `MONGODB_TEST_ALLOW_REMOTE`
(see `tests/README.md`).

Generate `ENCRYPTION_KEY` once:

```
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Run with docker compose

```
cp .env.example .env        # then edit .env with real values (it is git-ignored)
docker compose up -d --build bot
docker compose logs -f bot  # JSON log lines; secrets are redacted
docker compose ps           # the container reports healthy once the health endpoint answers
```

The image runs as an unprivileged user (uid 10001), with a read-only filesystem, no Linux
capabilities and `no-new-privileges`. The health endpoint (`GET /`) is bound to localhost.

## Run with plain docker

```
docker build -t prime-subs-bot .
docker run -d --name prime-subs-bot --restart unless-stopped --env-file .env \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
  -p 127.0.0.1:8080:8080 prime-subs-bot
```

## First start checklist

1. `MONGODB_URI` is reachable (Atlas: the host's IP is allowed in Network Access).
2. The first start creates the indexes and refuses to run if existing data has duplicate
   payment proofs (it prints the affected order ids).
3. Send `/start` to the bot, then place a test order and approve it from the admin account.
4. Optionally run the read-only seller check (it never places an order):
   `SUPPLIER_2_API_KEY=... python scripts/seller_contract_check.py`.

## Stopping and restarting

Stopping mid-purchase is safe: on the next start, orders left `fulfilling` become
`reconcile/unknown` and the admin is alerted with the supplier client reference to check.
Orders that were purchased but not delivered (`purchased_undelivered`) are delivered
automatically after a restart.
