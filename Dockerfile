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

# Gunicorn: single worker + 50 threads — required for SSE session affinity.
# Shell form so ${PORT} is expanded at runtime; Railway sets PORT, local falls back to 8000.
CMD gunicorn publive_mcp.wsgi -w 1 --threads 50 -b 0.0.0.0:${PORT:-8000} --timeout 60 --access-logfile -
