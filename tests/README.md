# Tests

```
pip install -r requirements-dev.txt
pytest
```

All tests are offline: an in-memory MongoDB (mongomock-motor), a fake Pandora and fake Telegram
objects. The project's real `.env` is never read.

## Real MongoDB (optional)

`tests/test_recovery.py` runs every test twice: on the in-memory database, and on a **real**
MongoDB when `MONGODB_TEST_URI` is set. Without it the real-database runs are skipped.

```
docker compose --profile test up -d mongo-test      # throwaway server on 127.0.0.1:27018
MONGODB_TEST_URI=mongodb://127.0.0.1:27018 pytest
docker compose --profile test down
```

Safety: each test gets its own randomly named database (`psb_test_<random>`) which is dropped
afterwards, and URIs containing `mongodb.net` (Atlas) are refused unless
`MONGODB_TEST_ALLOW_REMOTE=1`. Never point it at production data.
