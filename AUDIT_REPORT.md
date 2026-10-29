# AUDIT_REPORT.md - prime-subs-bot (read-only technical audit)

Audit basis: the working tree at audit time (3 commits + uncommitted changes), read statically. No real message was sent, no order placed, no supplier API called, no database contacted. No secret values appear in this report; where a secret lives only the variable NAME and file/line are given. Line numbers refer to `bot.py` unless a file is named.

Legend: **VERIFIED** = confirmed by reading code or running a local check. **ASSUMPTION** = reasoned, not observed. **NOT PRESENT** = does not exist in the repo.

---

---

## PHASE 0 / PHASE 1 STATUS UPDATE (branch `fix/phase1`)

The sections below describe the tree **as audited** (commit `00371eb`); the file/line references in them have since shifted. This section records what the `fix/phase1` branch changed. Everything was verified offline only: 47 pytest tests (in-memory MongoDB, fake Pandora, fake Telegram), a clean install in a brand-new venv (`pip install -r requirements-dev.txt`, `pip check` clean), and a startup check with dummy values against an unreachable local MongoDB. Nothing was run against real MongoDB, Telegram or either seller.

| Commit | Change |
|---|---|
| Phase 0 | All dependencies exact-pinned (incl. `pymongo==4.19.0`); `requirements-dev.txt` for the tests. `.env.example` already carried the `MONGODB_URI` placeholder. |
| Fix 1 | Suppliers count only if live. `CANBOSO_ENABLED` (default off) AND `CANBOSO_PURCHASE_IMPLEMENTED` (False in code) gate Canboso; catalog, pricing and fulfilment ignore it, and startup refuses `CANBOSO_ENABLED=true` while the purchase is a stub. |
| Fix 2 | Every post-claim write filters on status AND `claim_token` (`persist_order` requires the token; `claim_token` cannot be overwritten through fields). |
| Fix 3 | Credentials are encrypted (`ENCRYPTION_KEY`, Fernet, comma-separated keys for rotation) and stored on the order with status `purchased_undelivered` BEFORE delivery; only then sent, then `delivered`. Failed deliveries stay `purchased_undelivered`; a background pass redelivers (min age 120 s, 5 attempts, then an admin alert with the credentials). |
| Fix 4 | The unique index on the canonical payment proof already existed, but `0x<txid>` and `<txid>` were treated as different (reproduced). The canonical form now drops the `0x` prefix; the duplicate message is explicit; a duplicate-key race is handled the same way. |
| Fix 5 | The admin approval message has an EXPECTED PAYMENT block (amount, coin/network, the address/ID the customer was told to pay, snapshotted as `pay_to`) next to the SUBMITTED PROOF. |

**New or changed environment variables (names only):** `ENCRYPTION_KEY` (required), `CANBOSO_ENABLED` (optional, keep `false`), and `MONGODB_URI` is required as before. **New order statuses/fields:** status `purchased_undelivered`; fields `credentials_enc`, `purchased_at`, `delivered_at`, `delivery_attempts`, `pay_to`.

**Resolution of the 27 problems in section 9 after Phase 0/1:** 2 resolved (#1, #5), 7 partially resolved (#2, #4, #6, #7, #9, #22, #23), 1 mitigated but still open (#3), 17 open. See the Phase 3b section for the current totals.

**Known limits of this verification:** the new `ENCRYPTION_KEY`/`MONGODB_URI` values must be added to your real `.env` by you; Pandora and Canboso behaviour is still unverified against the real APIs. (An earlier version of this note said the in-memory MongoDB cannot run the startup-recovery pipeline update. That was wrong: it can, and Phase 3 Item 1 tests recovery on it and on a real server.)

---

## PHASE 3b STATUS UPDATE (branch `fix/phase3b`, built on `fix/phase3`)

Ten items, one commit each, with tests (310 pass, 58 skipped, run offline). The skipped tests are the real-MongoDB runs: most of the new tests run on both the in-memory database and, when `MONGODB_TEST_URI` is set, a real one. No real server was available here, so **none of those real-database runs has been executed yet**; CI runs them and fails if the server is missing. #3 (payment verification) and #9 (refunds) were deliberately not touched.

| Item | Problem | Change |
|---|---|---|
| 1 | #26 | `ADMIN_CHAT_ID` must be a positive whole number; a group id, `@username`, `0` or anything else stops startup with a message that says what to fix (the value is not echoed). |
| 2 | #8 | Admin alerts and customer notices for held, reconciled and recovered orders are recorded on the order in the same write as the state change (`alert_pending`, `notice_pending`), sent immediately, retried with exponential backoff (60 s doubling to 1 h) and re-sent after a restart. They are rebuilt from the current state (an unlock button only for an unknown outcome, never for a purchased order; nothing if the order moved on). Recovered and held orders now notify the customer. |
| 3 | #10 | A buyer picking a payment method for a product with known stock of 50 or fewer gets a 15-minute reservation of one unit (atomic slot claim, same buyer gets the same unit, cancel releases it); if none is left they get a clear message. The reservation is attached to the order when the proof arrives (a proof after expiry still creates the order, flagged to the admin) and released on rejection, hold, reconciliation and completed purchase. |
| 4 | #11 | If the live cost is above what the order was priced on, the order is no longer failed: it goes to `price_review` and the admin sees the cost when ordered, the cost now, the difference, the margin if bought now against at order time and a LOSS warning, and chooses Approve at new cost or Hold. `actual_cost` now records what the supplier really charged. |
| 5 | #20 | `MongoFSMStorage` keeps checkout, search and unlock-flow state in MongoDB (TTL 7 days); verified by resuming each flow through brand-new storage objects. |
| 6 | #21 | Compound indexes for the status-based scans; the startup reference audit reads only orders that are not finished, through a hinted status-index scan. The whole collection is scanned once, only if the unique index cannot be built because of existing duplicates. |
| 7 | #16 | Admin `/unblock <payment proof> <reason>` frees the proof of a rejected order; the reason is required and an audit-log entry (admin, time, order, reason, hash of the proof) is written before and completed after the change. |
| 8 | #17 | `Retry-After` is parsed as seconds or an HTTP date, capped at 30 s, default 5 s. |
| 9 | #25 | `.vscode` untracked and ignored; `orders.json`/`prefs.json` removed from `.gitignore` (the secret scan still flags them if committed). |
| 10 | #24, #27 | The Bot session is closed on every exit path; message limits are counted in UTF-16 units. |

**Resolution of the 27 problems in section 9 (current):** 18 resolved (#1, #5, #7, #8, #10, #11, #12, #14, #16, #17, #20, #21, #22, #23, #24, #25, #26, #27), 6 partially resolved (#2, #4, #6, #9, #13, #18), 1 mitigated but still open (#3), 2 open (#15, #19). Each item in section 9 carries its tag.

**Not verified in this session:** the real-MongoDB runs of the tests above (including the concurrent-reservation race and the query-plan check); Docker and the GitHub Actions workflow (as before); behaviour against the live Pandora API. New collections: `reservations`, `fsm`, `audit_log`; new order status: `price_review`; new order fields: `price_review`, `approved_cost`, `alert_*`, `notice_*`, `stock_reservation`, `unblocked_*`.

---

## PHASE 3 STATUS UPDATE (branch `fix/phase3`, built on `fix/phase1`)

Eight items, one commit each, with tests (161 pass, 13 skipped, run offline). The 13 skipped are the real-MongoDB runs of the recovery tests: they need `MONGODB_TEST_URI`, which was not available here (no Docker, no `mongod`), so **those runs have not been executed yet**. CI runs them against a real MongoDB service and fails if it cannot.

| Item | Change |
|---|---|
| 1 | `tests/test_recovery.py`: startup recovery in every order state (fulfilling, fulfilling+purchased, fulfilling after an unlocked retry; pending/held/reconcile/delivered/rejected/delivery_failed/purchased_undelivered untouched), idempotency, what a recovered order may do next, the restart sequence for `purchased_undelivered`, a concurrent-claim race and the unique-proof index. Every test runs on the in-memory database and, when `MONGODB_TEST_URI` is set, on a real one (random database, dropped afterwards; Atlas refused unless allowed). `docker-compose.yml` has a throwaway `mongo-test` service. |
| 2 | `@dp.errors()` handler: full traceback in the logs, a neutral reply to the user, a short throttled admin alert (exception type and update kind only). |
| 3 | `ratelimit.py`: 8 messages / 10 s and 15 button presses / 10 s per user, and 5 payment-proof attempts / 10 min (checked before any database work); admin exempt. |
| 4 | `logsetup.py`: JSON-lines logs with `order_id` on every line of an order flow, and secret redaction (exact environment values plus token/key/URI patterns) on message and traceback. A test greps the output of failure and success flows for secrets, credentials and payment proofs. |
| 5 | `scripts/seller_contract_check.py`: read-only Pandora check (GET endpoints and error probes; `POST /quotes` only with `--with-quote`; never an order), prints shapes/status/error codes with the key masked. `--dry-run` sends nothing. |
| 6 | `Dockerfile` (non-root uid 10001), `docker-compose.yml` (read-only, no capabilities, localhost health port), `.dockerignore`, `DEPLOY.md` with every environment variable name. |
| 7 | `scripts/backup_mongo.sh` (timestamped, compressed, owner-only, URI never on the command line) and a backup/restore section in `DEPLOY.md`, stating that `ENCRYPTION_KEY` is not in the backup and must be stored separately. |
| 8 | `.github/workflows/ci.yml`: tests against a real MongoDB service plus `scripts/secret_scan.py --history` on every push and pull request. |

**Resolution of the 27 problems in section 9 after Phase 3 (superseded by the Phase 3b section):** 7 resolved (#1, #5, #7, #12, #14, #22, #23), 6 partially resolved (#2, #4, #6, #9, #13, #18), 1 mitigated but still open (#3), 13 open (#8, #10, #11, #15, #16, #17, #19, #20, #21, #24, #25, #26, #27). Each item in section 9 carries its tag.

**Not verified in this session:** the Docker image was not built and `docker compose` was not run (no Docker here); the GitHub Actions workflow has not run (it is checked only for structure and YAML validity); `scripts/backup_mongo.sh` was tested against a stub `mongodump`, not the real tool; the contract check was only dry-run and tested with a fake session, never against the live Pandora API.

---

## 1. OVERVIEW

**Language / framework**
- Python (local venv is 3.12.10; the code needs 3.10+ because of `X | None` annotations, e.g. bot.py:117, 434). No Python version is pinned anywhere.
- aiogram 3.13.0 (long polling), aiohttp 3.10.11 (health server), requests 2.31.0 (supplier HTTP, blocking, run in worker threads), python-dotenv 1.0.1, PyMongo `>=4.13,<5` (async API, `AsyncMongoClient`).
- Installed in `.venv`: aiogram, aiohttp, requests, python-dotenv (+ pydantic 2.8.2, magic-filter, certifi). **`pymongo` is NOT installed in `.venv`** (VERIFIED).

**Files**
| File | Purpose |
|---|---|
| `bot.py` (1739 lines) | Entire bot: config, supplier clients, catalog merge/pricing, customer flows, admin approval/fulfilment, reconciliation, alerts, health server, `main()` |
| `storage.py` (442 lines) | MongoDB layer: orders, language prefs, atomic claim/reject/unlock, indexes, startup recovery, notification marker |
| `requirements.txt` | 5 dependencies |
| `.env.example` | Placeholder-only variable list |
| `.env` | Real local config (untracked, git-ignored) |
| `.gitignore` | Ignores `.env`, `__pycache__/`, `*.pyc`, `.venv/`, `orders.json`, `prefs.json` (the last two are stale: JSON storage no longer exists) |
| `.vscode/settings.json` | Tracked editor setting (`python.terminal.useEnvFile`) |
| `AUDIT_REPORT.md` | This file |

**Entry point / start**: `python bot.py` -> `asyncio.run(main())` (bot.py:1739). `main()` (bot.py:1661) validates env, connects MongoDB, builds indexes, loads language prefs, creates the `Bot`, registers menu commands (1699), recovers interrupted orders, starts the health server and a notification retry task, then **long polling** (`dp.start_polling`, 1729). **Webhook mode: NOT PRESENT.** An aiohttp health server (`GET /` -> "Bot is running") listens on `PORT` (default 8080), bot.py:1646-1660.

**Dependencies**: all five are imported and used; no unused dependency (VERIFIED). Whether newer/vulnerable versions exist was **not checked** (needs network).

---

## 2. FEATURES (what exists in code)

**Customer commands** (BOT_COMMANDS bot.py:655): `/start` (680), `/menu`, `/products` (855), `/orders` (719), `/search` (899), `/support` (746), plus unlisted `/language` (761) and `/cancel` (1036).

**Customer button flows**
- Main dashboard grid: Store, Search, Orders, Support, Language (main_menu, 641).
- Store: paginated catalog, 8 per page with "n / m" (836-852), product detail with Buy Now (873-897). Search: keyword over name/description with paging (915-962).
- Buy: payment-method buttons (966-990), instructions (address / Binance ID) and reference prompt (993-1026), Cancel (1029). Reference submission creates a `pending` order (1043-1109).
- Orders: last 5 orders with status and, for delivered orders, the credentials (700-730).
- Support: static text/link from `SUPPORT_USERNAME` (735-743). Language: English/Hindi/Spanish/Chinese; **only the dashboard strings are translated** (576-617).

**Admin** - identified by comparing the Telegram user id to `ADMIN_CHAT_ID` (`is_admin`, 552-553). **There are no admin slash commands (NOT PRESENT).** Admin actions are inline buttons on alert messages: Approve/Retry (1147), Reject (1117), Unlock (1445), unlock note + final confirm (1479, 1527), legacy `unlockyes` stub (1570), cancel (1579).

**Status of each area**
- Fully built: catalog/search/pagination, order submission, admin approve/reject, Pandora purchase + delivery, My Orders, language selection, MongoDB persistence, reconciliation + Pandora-confirmation unlock flow, new-order alert outbox.
- Stubbed: **Canboso purchase** - `purchase_canboso` always raises (272-274).
- Partial: translations (dashboard only); support (static contact text).
- NOT PRESENT: balance/wallet, refunds, quantity > 1, user registration/profile, admin statistics/broadcast/product management, coupons, order cancellation by customer.

---

## 3. SELLER API INTEGRATION

**Seller 1 - "Canboso"** (X-API-Key header)
- Base: `https://canboso.com/api/v2/telegram-buyer/products` (37). Endpoint used: `GET .../products` (149-174). Fields parsed: `productId`, `name`, `description`, `price`, `availability.available`.
- Fetch: in-stock only (`available > 0`); price extracted from several possible shapes (`parse_price`, 137-146).
- **Purchase / delivery: NOT IMPLEMENTED** (272-274). Response shapes are guesses; I could not reach this API (ASSUMPTION).

**Seller 2 - "Pandora Digital"** (Bearer token)
- Base: `https://api.pandoradigital.shop/api/v1` (41). Endpoints used: `GET /products` (cursor pagination, max 20 pages, 189-218), `POST /quotes` (294), `POST /orders` with `Idempotency-Key: bot-<ref>` and `client_order_reference` (325), `GET /orders/{id}` polling (357).
- Order flow (277-371): quote -> check `can_purchase` -> check quote price <= cost ceiling (303) -> POST order (up to 3 attempts for timeout/connection/429/5xx, 316-337) -> poll up to 90 s at 3 s intervals (351-361) -> `delivered` returns `delivery.items` as credentials.
- Delivery: credentials come from the `/orders` response; `approve_order` (1147-1352) stores them on the order and sends them to the customer in Telegram.

**Timeouts**: catalog 15 s (73), purchase calls 30 s (74), ownership check 20 s (75).

**If the seller API fails** (all in `approve_order` / `fulfil`, 377-391, 1147-1352):
- Failure *provably before* any purchase request (quote errors, `can_purchase` false, price above ceiling, clean first-attempt 4xx, supplier says `failed`) -> `SupplierError` -> order `held` (`outcome=definitive_failure`), `attempt+1`, admin alert with Retry/Reject, customer told delivery is delayed.
- Anything uncertain (timeouts after a request may have been sent, 429/5xx, key conflict, polling timeout, unexpected exception) -> `AmbiguousOrderError` -> order `reconcile` (`outcome=unknown`), Retry/Reject blocked, admin alert, manual Pandora-confirmed unlock required (see section 4).
- There is **no automatic retry** of an unknown outcome and no refund mechanism (NOT PRESENT).

**Stock / price sync**: no background job (NOT PRESENT). The catalog is built on demand and cached 30 s (`CATALOG_TTL` 76, `get_catalog` 254-261); it is force-refreshed on Buy and on payment-method choice (976, 1003). Pandora stock `null` is treated as in stock (204-207); no stock reservation.

**Margin**: `retail = lowest_supplier_cost * (1 + MARKUP_PERCENT/100) + MARKUP_FIXED` (264-265; defaults 40 / 0, 44-45). Products from both sellers are merged by exact normalized name and sorted cheapest-first (133-135, 223-251). The price is fixed into the order at payment time; the purchase is refused if the supplier's current price exceeds the ceiling derived from that price (381, 303).

---

## 4. PAYMENTS AND BALANCE

**Methods** (48-67): Binance Pay (ID/email), BEP20 USDT address, TRC20 USDT address. Each is a manual transfer to a configured destination; a method is only offered if its env value is set. Amount is shown in USDT; supplier prices are treated as the same number (USD, ASSUMPTION).

**Verification: manual admin approval only.** The bot checks only the *format* of the user-supplied reference (`TXID_RE` / `BINANCE_ORDER_RE`, 69-70, 1049-1054) and that it is not already used (`storage.reference_exists`, storage.py:424; unique partial index storage.py:217). It does **not** verify amount, destination, sender or that the transaction exists (no blockchain/Binance API; NOT PRESENT). The admin receives the reference in the alert (`new_order_alert`, 1593) and decides.

**Balance / wallet**: NOT PRESENT. Negative-balance bugs are therefore not applicable.

**Ways to get a product without confirmed payment**
- Only through admin approval: a customer can submit any well-formed fake or someone else's reference; if the admin approves without checking, the product is delivered (HIGH, by design).
- No non-admin path delivers a product (VERIFIED: purchase is only reachable from `approve_order`, gated by `is_admin` and the atomic claim).

**Double delivery**
- Double Approve taps: prevented by the atomic claim (storage.py:282).
- Possible with overlapping bot instances or an admin unlock+Retry while an older request is still in flight (see Problems #5, #6): two purchases and two credential sets.

---

## 5. DATABASE / STORAGE

**MongoDB** (Atlas assumed) via PyMongo async, database `MONGODB_DB` (default `prime_subs_bot`). **Collections: `orders`, `prefs`.** Users, products, transactions and balances are NOT stored (NOT PRESENT): the catalog lives in memory, users exist only as `user_id` on orders and prefs.

**`orders` document fields**: `_id`/`id` (10-hex), `status` (pending, held, fulfilling, reconcile, delivered, delivery_failed, rejected), `outcome` (definitive_failure, unknown, purchased, verified_none), `user_id`, `user_label`, `name`, `price`, `method`, `reference`, `reference_lower`, `offers[]` (supplier, product_id, variant_id, cost), `attempt`, `error`, `supplier`, `actual_cost`, `credentials[]` (**plaintext**), `claim_token`, `created_at`, `admin_notified` (+`admin_notified_at`), `reconciled_by`, `reconciled_at`, `reconciliation_note`. **`prefs`**: `_id` = user id string, `lang`.

**Indexes** (storage.py:193, 217): `(user_id, created_at desc, _id desc)` and a unique partial index on `reference_lower`. **Missing** (LOW): index for the `status`-based scans (`recover_interrupted_orders` 356, `pending_unnotified` 390).

**Transactions / locks**: no multi-document transactions; correctness relies on single-document atomic operations (`find_one_and_update` claim/reject/unlock, conditional `update_one`). No application-level locks.

**Backup**: NOT PRESENT in the repo. Backups depend entirely on the Atlas plan (a free tier has no cloud backups - ASSUMPTION).

---

## 6. SECURITY

**Hardcoded secrets**: none found. A regex scan of `bot.py`, `storage.py`, `.env.example` for bot-token, `sk_live_`, Mongo-URI-with-credentials patterns found 0 matches (VERIFIED). Config comes from env (`load_dotenv`, bot.py:30). Secret-bearing variable NAMES: `BOT_TOKEN`, `SUPPLIER_API_KEY`, `SUPPLIER_2_API_KEY`, `MONGODB_URI`, payment destinations.

**`.gitignore` / history**
- `.env` is untracked and ignored (`git check-ignore` -> `.gitignore:1`; never in any commit's file list). VERIFIED.
- History scan over all 3 commits with `git log -p`: 0 matches for token/`sk_live_`/Mongo-credential/AWS-key/header-key patterns. The only hits for `SUPPLIER..._API_KEY =` / `BOT_TOKEN =` were `os.getenv(...)` assignments (checked: no non-`getenv` assignment exists). VERIFIED. Values masked; none printed.

**Input validation**: payment reference regex (69-70); admin unlock note 3-200 chars (1479+); language and payment-method values validated against allow-lists; search text has no length cap (LOW). No regex is built from user input.

**Injection**: no SQL, no shell execution, no `eval`; Mongo filters are built from fixed dicts with user text only as string values (VERIFIED).

**Admin access control**: single numeric Telegram user id (`ADMIN_CHAT_ID`, 552-553), checked on every admin callback and on the unlock note. If a group/channel id is configured, admin actions never match (config pitfall).

**Rate limiting / spam protection**: NOT PRESENT (no throttling or middleware, VERIFIED by search).

**Other**: *(UPDATED on fix/phase1: credentials are now stored Fernet-encrypted in `credentials_enc`, key from `ENCRYPTION_KEY`.)* At audit time credentials were stored in plaintext in MongoDB (1225-1231) so "My Orders" can show them. Payment references are no longer written to logs (the audit log was fixed; every log call uses order ids / error types).

---

## 7. RELIABILITY AND OPS

- **Logging**: `logging.basicConfig(level=INFO)` to stderr only (1662). No file logging, no alerting beyond Telegram admin messages. Supplier/DB errors are logged by error type, not message.
- **Crash handling**: no global aiogram error handler (NOT PRESENT); handler exceptions are only logged by aiogram. Purchases are guarded after the claim (`approve_order` phases). Startup failures exit with `SystemExit` (1664-1705).
- **Restart behaviour**: no supervisor in the repo. On start, orders left `fulfilling` become `reconcile/unknown` and the admin is alerted (1703-1722). FSM state (checkout, search, unlock flow) is in memory (`Dispatcher()` default storage, 78) and is lost on restart.
- **Environment variables (names only)**

| Variable | Needed | Purpose |
|---|---|---|
| `BOT_TOKEN` | required | Telegram bot token |
| `ADMIN_CHAT_ID` | required | Admin Telegram user id |
| `SUPPLIER_API_KEY` | one supplier key required | Canboso key |
| `SUPPLIER_2_API_KEY` | one supplier key required | Pandora key |
| `MONGODB_URI` | required | MongoDB connection string |
| `MONGODB_DB` | optional (default `prime_subs_bot`) | Database name |
| `MARKUP_PERCENT`, `MARKUP_FIXED` | optional (40 / 0) | Margin |
| `BINANCE_PAY_ID`, `BEP20_ADDRESS`, `TRC20_ADDRESS` | at least one to take payments | Payment destinations |
| `SUPPORT_USERNAME` | optional | Support contact link |
| `PORT` | optional (8080) | Health server port |

  The local `.env` has `BOT_TOKEN`, `SUPPLIER_API_KEY`, `SUPPLIER_2_API_KEY`, `MARKUP_PERCENT`, `MARKUP_FIXED`, `ADMIN_CHAT_ID`, `BINANCE_PAY_ID`, `BEP20_ADDRESS`, `TRC20_ADDRESS`, `SUPPORT_USERNAME` set (all non-empty) and **no `MONGODB_URI` / `MONGODB_DB`**.
- **Deployment files**: Dockerfile, Procfile, `render.yaml`, systemd unit, CI config - all NOT PRESENT. The only host hint is the health server comment mentioning Render (bot.py:1647). No start command or persistent-disk requirement is documented.
- **Tests**: *(UPDATED on fix/phase1: 47 offline tests in `tests/`, run with pytest; see the status section.)* At audit time: NOT PRESENT (no test files, no pytest/tox config). No linter or type-checker is installed in the venv.

---

## 8. HOW TO RUN

1. Python 3.10+ (venv is 3.12.10): `python -m venv .venv` then activate it.
2. `pip install -r requirements.txt` (adds `pymongo`).
3. Copy `.env.example` to `.env` and fill in real values, including `MONGODB_URI`.
4. `python bot.py`.

**Does it run right now? At audit time: No.** *(UPDATED on fix/phase1: dependencies are pinned and a fresh venv installs; the bot still needs `MONGODB_URI` and `ENCRYPTION_KEY` in `.env`.)* (VERIFIED, safe check: `import bot` with the project venv and no secrets):
- `ModuleNotFoundError: No module named 'pymongo'` at bot.py:26.
- Even after installing it, `.env` has no `MONGODB_URI`, so `main()` would exit with "MONGODB_URI environment variable is not set." (storage.py:112, bot.py:1674).

**Static checks that were possible** (no packages installed, nothing contacted): both files compile; an AST scan found no undefined names and no unused imports; every callback prefix produced has a handler. Not possible: running the bot, importing `storage.py`, or any test (no tests; `pymongo` missing).

---

## 9. PROBLEMS FOUND

**CRITICAL**
1. **[RESOLVED (Fix 1)]** **Products where Canboso is the cheapest source can never be fulfilled, but are sold at the Canboso-based price.** `purchase_canboso` always fails (272-274); the price is derived from the cheapest offer (264, `Listing.cost` 128-130); the Pandora fallback refuses to buy when its price exceeds the ceiling computed from that price (381, 303). Failure path: customer pays -> admin approves -> Canboso stub fails -> Pandora is dearer than the ceiling -> order is `held`; Retry fails identically. (VERIFIED by code; ASSUMPTION that Pandora's cost exceeds Canboso's for those products.)

**HIGH**
2. **[PARTIALLY RESOLVED (Phase 0: deps pinned, fresh install verified; your .env still needs MONGODB_URI and the new ENCRYPTION_KEY)]** The bot does not start in the current environment: `pymongo` missing (bot.py:26) and no `MONGODB_URI` in `.env` (storage.py:112).
3. **[MITIGATED, STILL OPEN (Fix 4+5: one proof backs one order incl. the 0x bypass; admin sees amount/network/proof; still no automated on-chain/Binance verification)]** Payments are not verified by the bot: any well-formed reference is accepted (69-70, 1049-1075); amount, destination and sender are never checked; safety depends entirely on the admin's manual check.
4. **[PARTIALLY RESOLVED (Fix 3: credentials are stored encrypted BEFORE delivery; remaining: database + Telegram + customer delivery all failing at purchase time)]** Credentials can be lost if a purchase succeeds while MongoDB, Telegram and customer delivery all fail (1224-1262, 1274-1278). Credentials then exist only in memory/the alert; recovery would be from the supplier by client reference (retention undocumented).
5. **[RESOLVED (Fix 2)]** Post-claim writes are conditional on status only, not on the claim token (1232, 1236, 1360, 1385; storage.py:248-280). A late handler from an older attempt/instance can write onto a newer attempt's `fulfilling` order.
6. **[PARTIALLY RESOLVED (Fix 2 stops stale writes; a duplicate purchase through overlap/unlock still depends on the admin's Pandora confirmation)]** Overlapping instances / unlock-and-Retry can cause a second purchase: startup recovery flips every `fulfilling` order (storage.py:356-388), the fence only blocks *new* requests, and unlock relies on the admin's Pandora confirmation (1445-1567; storage.py:324-354). Safety depends on that human confirmation.

**MEDIUM**
7. **[RESOLVED in the repo (Fix 3 encrypts credentials at rest; Phase 3 Item 7 adds scripts/backup_mongo.sh and restore docs). Running it on a schedule, copying backups off-site and backing up ENCRYPTION_KEY separately is up to the operator]** No backup/restore mechanism in the repo; orders and plaintext credentials (1225-1231) live only in MongoDB.
8. **[RESOLVED (Phase 3b Item 2: alerts and customer notices are owed on the order, retried with backoff and re-sent after a restart; a customer who cannot be reached is given up on after 8 attempts)]** Startup-recovery, hold and reconcile alerts are sent once and not retried if Telegram fails (1370-1378, 1382-1415, 1706-1722); recovered orders' customers are not notified.
9. **[PARTIALLY RESOLVED (Fix 3: a paid-but-undelivered order shows Processing and is redelivered automatically; refunds/compensation still NOT PRESENT)]** No refund/compensation path for held, rejected or failed orders (NOT PRESENT); `delivery_failed` shows the paying customer "Failed" (671, 713).
10. **[RESOLVED for products with known stock of 50 or fewer (Phase 3b Item 3: 15-minute reservation, clear 'stock gone' message; unknown or plentiful stock is not reserved)]** Stock race: no reservation; the last unit can be sold twice and the second order is held after payment (204-207, 976, 1003).
11. **[RESOLVED (Phase 3b Item 4: a cost rise parks the order for the admin, with new cost, margin and difference, to Approve at new cost or Hold)]** Price drift between payment and approval makes the cost-ceiling check fail and the order hold (303, 381); there is no repricing.
12. **[RESOLVED (Phase 3 Item 3: per-user limits on messages, button presses and payment-proof attempts; admin exempt)]** No rate limiting; each tap can trigger supplier catalog fetches (254-261, 976, 1003) and database reads.
13. **[PARTIALLY RESOLVED (Phase 3 Item 5: scripts/seller_contract_check.py can verify Pandora's shapes and error codes read-only; it has NOT been run against the live API, and Canboso is still unverified)]** Seller behaviour is unverified: Pandora idempotency and error-code classification (342) rely on documentation not in the repo; Canboso response shapes are guessed (137-174).
14. **[RESOLVED (Phase 3 Item 2: global error handler with traceback logging, a neutral user reply and a short throttled admin alert)]** No global error handler; some user-facing failures are silent (e.g. a `TypeError` from a bad supplier payload escapes `build_catalog`, 234).

**LOW**
15. **[OPEN]** Duplicate new-order alert possible while the first alert is still in flight (1076 vs 1097; 1614-1643).
16. **[RESOLVED (Phase 3b Item 7: admin /unblock with a required reason and an audit-log entry)]** A rejected order's reference stays blocked forever (storage.py:424).
17. **[RESOLVED (Phase 3b Item 8: Retry-After is read as seconds or an HTTP date)]** `Retry-After` parsed with `int()` raises on an HTTP-date header (332) - fails safe to `unknown`.
18. **[PARTIALLY RESOLVED (Phase 3 Item 4: the traceback is still logged, but every log line is now redacted for secrets)]** Language-save failure logs a full traceback (800).
19. **[OPEN]** Translations cover only the dashboard (576-617).
20. **[RESOLVED (Phase 3b Item 5: conversation state is stored in MongoDB)]** FSM state lost on restart (78).
21. **[RESOLVED (Phase 3b Item 6: status-scan indexes; startup audits only unfinished orders)]** Missing indexes for status-based scans (storage.py:356, 390); startup reference audit scans every order (storage.py:152-183).
22. **[RESOLVED (every dependency is exact-pinned; the Dockerfile and CI pin Python 3.12)]** No Python version pin; `pymongo` has no exact pin (`requirements.txt`).
23. **[RESOLVED (47+ offline tests, Dockerfile, docker-compose.yml, DEPLOY.md and a GitHub Actions workflow with tests and a secret scan; the image and the workflow have not been run here)]** Deployment files, CI and tests absent; health server is unauthenticated on `0.0.0.0` (1646-1660).
24. **[RESOLVED (Phase 3b Item 10: the Bot session is closed on every exit path)]** `Bot` session is not closed on `SystemExit` paths (1685-1705) - cosmetic.
25. **[RESOLVED (Phase 3b Item 9: .vscode untracked and ignored; stale JSON entries removed)]** `.vscode/settings.json` is tracked; `.gitignore` still lists `orders.json` / `prefs.json`.
26. **[RESOLVED (Phase 3b Item 1: ADMIN_CHAT_ID must be a positive numeric user id, checked at startup)]** Admin id must be a user id, not a group id (552-553).
27. **[RESOLVED (Phase 3b Item 10: lengths are counted, truncated and split in UTF-16 units)]** Telegram length limits are approximated in code points (475, 522, 716).

---

## 10. OPEN QUESTIONS

1. Where will this be hosted (Render, VPS, other)? Is there a persistent disk, how is the process supervised, and will two instances ever run at once during deploys?
2. Which MongoDB plan/cluster will be used, and who owns backups?
3. Should payments stay manually verified, or is a gateway / on-chain verification (and which one) intended?
4. Do you have Canboso's order-creation API documentation? Until then, how should products where Canboso is cheaper be priced or hidden?
5. What is the policy for refunds, rejected payments and orders held after payment?
6. Is a customer-facing wallet/balance planned, or is the model strictly pay-per-order?
7. Is the supplier price really in USD and equal to USDT as assumed?
8. Is one admin account enough, or are multiple admins / admin commands needed?
9. Which Python version will the host run?
10. Is any real order data already stored anywhere (a previous deployment, old JSON files)? None exists in this repo, and there is no import tool.
