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
| `ADMIN_CHAT_ID` | yes | Telegram **user** id of the admin (approves orders): a positive whole number. A group id, `@username`, `0` or anything else is rejected at startup |
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

## Backup and restore

### Take a backup

Needs `mongodump` (MongoDB Database Tools) on the machine that runs it.

```
MONGODB_URI='mongodb+srv://...' ./scripts/backup_mongo.sh
```

This writes `backups/<db>_<UTC timestamp>.archive.gz` (+ a `.sha256`), private to the owner. The
URI is passed to `mongodump` through a short-lived private config file, never on the command
line. Useful variables: `MONGODB_DB`, `BACKUP_DIR`, and `BACKUP_RETENTION_DAYS` (prunes this
script's own old backups). Example nightly cron entry:

```
17 3 * * *  cd /opt/prime-subs-bot && set -a && . ./.env && set +a && BACKUP_RETENTION_DAYS=30 ./scripts/backup_mongo.sh
```

A backup contains customer ids, payment proofs and **encrypted** credentials. Copy backups off the
machine (and ideally encrypt them there), and treat them as sensitive. A free Atlas cluster has no
automatic backups, so run this on a schedule.

### ENCRYPTION_KEY must be backed up separately

Purchased credentials are stored encrypted with `ENCRYPTION_KEY`, and **the key is not in the
backup**. Without it a restored database is useless for credentials: My Orders cannot show them
and automatic redelivery cannot decrypt them. So:

- Store `ENCRYPTION_KEY` in a password manager / secrets vault, **in a different place from the
  backups** (backups and key together defeat the encryption).
- If you rotate the key, put the new key first and keep the old one(s) after it, comma-separated,
  for as long as any backup or order still uses them.
- Test that the key you saved is the one in use before you need it.

### Restore

1. **Stop the bot.** Only one instance may run, and a running bot would write into the database
   while you restore.
2. Pick the archive and check it: `sha256sum -c <archive>.sha256`.
3. **Rehearse in a scratch database first** (restores the dump under a different name, leaving the
   live data alone):

   ```
   mongorestore --uri "$MONGODB_URI" --gzip --archive=backups/<file>.archive.gz --nsFrom 'prime_subs_bot.*' --nsTo 'prime_subs_bot_restore.*'
   ```

   Inspect `prime_subs_bot_restore` (order counts, a few documents), then drop it.
4. Restore for real. `--drop` replaces the existing collections with the backup's:

   ```
   mongorestore --uri "$MONGODB_URI" --gzip --archive=backups/<file>.archive.gz --nsInclude 'prime_subs_bot.*' --drop
   ```

5. Start the bot with the **same `ENCRYPTION_KEY`** (and `MONGODB_DB`). On start it recreates the
   indexes and recovers any order left `fulfilling` (they become `reconcile/unknown` and the admin
   is alerted).
6. **Reconcile the gap.** Orders placed or purchased after the backup are not in the restored
   data. Compare Pandora's order list for that period with the restored orders, using the supplier
   client references (`<order id>-<attempt>`), and handle any purchase the database does not know
   about by hand.
