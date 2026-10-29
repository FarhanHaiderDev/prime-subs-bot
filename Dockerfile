# Production image for the Telegram bot. No secret is baked in: configuration comes from
# environment variables at run time (see DEPLOY.md).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Unprivileged user: the bot never needs root.
RUN groupadd --system --gid 10001 bot \
    && useradd --system --uid 10001 --gid bot --no-create-home --shell /usr/sbin/nologin bot

WORKDIR /app

# Dependencies first, so code changes do not reinstall them.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Only the application modules (tests, scripts, .env and git data are excluded by .dockerignore).
COPY --chown=root:root *.py ./

USER bot

# The health server listens on $PORT (default 8080).
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/' % os.getenv('PORT', '8080'), timeout=3)"]

CMD ["python", "bot.py"]
