#!/bin/sh
# Entrypoint used by both docker-compose (dev) and production (Dokploy).
set -e

echo "==> Applying database migrations"
attempt=1
until python manage.py migrate --noinput; do
    if [ "$attempt" -ge 15 ]; then
        echo "Database is not reachable after $attempt attempts, giving up." >&2
        exit 1
    fi
    echo "Migration attempt $attempt failed, retrying in 3s..."
    attempt=$((attempt + 1))
    sleep 3
done

echo "==> Collecting static files"
python manage.py collectstatic --noinput --clear >/dev/null

exec "$@"
