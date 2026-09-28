# Backend image: migrate / ingest / admin. Wheels only (no compiler), so a slim base is enough.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 HOME=/tmp
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# The admin's CSS/JS are collected at build time; the values below exist only so the production safety checks in
# settings pass during this one command and are never used at run time.
RUN DJANGO_SECRET_KEY=build-time-only-not-a-secret-build-time-only DB_USER=build DB_PASSWORD=build \
    OBJECT_STORE_BACKEND=s3 python manage.py collectstatic --noinput --settings=config.settings.ops

RUN useradd --system --uid 10001 --no-create-home app
USER app
ENV DJANGO_SETTINGS_MODULE=config.settings.production
ENTRYPOINT ["python", "manage.py"]
CMD ["check"]
