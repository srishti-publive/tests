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
    # package metadata & cache remove

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ARG DJANGO_SECRET_KEY=build-time-placeholder-not-used-at-runtime
ENV DJANGO_SECRET_KEY=${DJANGO_SECRET_KEY}
ENV DJANGO_SETTINGS_MODULE=publive_mcp.settings
ENV DJANGO_ENV=production

# Collect static files at build time 
RUN python manage.py collectstatic --noinput

EXPOSE 8000

# Makes script executable
RUN chmod +x /app/entrypoint.sh
CMD ["/app/entrypoint.sh"]
