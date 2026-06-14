FROM python:3.12-slim

# Prevents Python from writing .pyc files and ensures stdout/stderr are unbuffered
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies: libpq-dev/gcc for psycopg2, redis-server to run
# an in-container Redis (sessions, queues, stats, rate limits) instead of an
# external instance.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    redis-server \
    && rm -rf /var/lib/apt/lists/*
    # package metadata & cache remove

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ARG DJANGO_SECRET_KEY=build-time-placeholder-not-used-at-runtime
ENV DJANGO_SECRET_KEY=${DJANGO_SECRET_KEY}
ENV DJANGO_SETTINGS_MODULE=publive_mcp.settings.prod

# Collect static files at build time.
# collectstatic never touches Redis, but importing the prod settings triggers the
# fail-fast REDIS_URL guard. Supply a throwaway value inline so it stays build-only
# (NOT an ENV) — a persisted placeholder would mask a missing real REDIS_URL at runtime.
RUN REDIS_URL="redis://build-time-placeholder:6379/0" python manage.py collectstatic --noinput

EXPOSE 8000

# Makes script executable
RUN chmod +x /app/entrypoint.sh
CMD ["/app/entrypoint.sh"]
