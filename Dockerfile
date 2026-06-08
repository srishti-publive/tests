FROM python:3.12-slim

# Prevents Python from writing .pyc files and ensures stdout/stderr are unbuffered
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies required by psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Collect static files at build time (requires DJANGO_SECRET_KEY to be set)
ARG DJANGO_SECRET_KEY=build-time-placeholder-not-used-at-runtime
ENV DJANGO_SECRET_KEY=${DJANGO_SECRET_KEY}
ENV DJANGO_SETTINGS_MODULE=publive_mcp.settings.prod

RUN python manage.py collectstatic --noinput

EXPOSE 8000

# Apply migrations then start gunicorn (single worker + 50 threads — staged at
# this count while the now-Redis-backed SSE session/queue/stats routing
# (mcp_app/transport/redis_session_store.py, redis_message_queue.py) is verified
# in production; see docs/deployment.md for the worker-count rollout plan).
# Migrations run in the entrypoint so they reliably apply on every deploy with
# visible logs; ${PORT} is expanded at runtime by the shell.
RUN chmod +x /app/entrypoint.sh
CMD ["/app/entrypoint.sh"]
