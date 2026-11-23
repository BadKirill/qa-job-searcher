#!/usr/bin/env bash
set -euo pipefail
umask 077
if [[ "${QA_BACKUP_LOCK_HELD:-0}" != 1 ]]; then
    exec 9>/run/qa-job-searcher-backup.lock
    flock -n 9
fi
cd /srv/qa-job-searcher
bucket=$(cat .runtime/backup-bucket)
name="daily-$(date -u +%Y%m%dT%H%M%SZ)"
services=(api scheduler search-worker chat-worker document-worker)
compose() {
    docker compose -p job-searcher-aws --env-file .runtime/runtime.env -f compose.runtime.yaml -f compose.aws.json "$@"
}
resume() {
    compose run --rm --no-deps -T maintenance python -m qa_job_bot.maintenance off
    compose up -d --no-build --wait --wait-timeout 180 "${services[@]}"
}
compose run --rm --no-deps -T maintenance python -m qa_job_bot.maintenance on
trap resume EXIT
compose exec -T api python - <<'PY'
import time
from sqlalchemy import func, select
from qa_job_bot import pg_schema as schema
from qa_job_bot.operations import ACTIVE
from qa_job_bot.pg_database import Database

database = Database.from_env()
deadline = time.monotonic() + 180
try:
    while True:
        with database.transaction() as connection:
            active = connection.execute(select(func.count()).select_from(schema.tasks).where(schema.tasks.c.status.in_(ACTIVE), schema.tasks.c.lease_until > time.time())).scalar_one()
        if not active:
            break
        if time.monotonic() >= deadline:
            raise SystemExit('Backup postponed because tasks still have active leases')
        time.sleep(3)
finally:
    database.close()
PY
compose stop "${services[@]}"
install -d -m 0700 .runtime/exports
compose run --rm --no-deps -T maintenance python - < verify_data.py > ".runtime/exports/$name.digest.json"
compose run --rm --no-deps -T maintenance python -m qa_job_bot.runtime_backup backup "/backups/$name" --services-stopped
compose run --rm --no-deps -T maintenance python - < verify_data.py > ".runtime/exports/$name.digest-after.json"
cmp -s ".runtime/exports/$name.digest.json" ".runtime/exports/$name.digest-after.json"
compose run --rm --no-deps -T maintenance tar -C /backups -czf - "$name" > ".runtime/exports/$name.tar.gz"
sha256sum ".runtime/exports/$name.tar.gz" > ".runtime/exports/$name.tar.gz.sha256"
resume
trap - EXIT
aws s3 cp ".runtime/exports/$name.tar.gz" "s3://$bucket/backups/$name.tar.gz" --region eu-north-1 --sse AES256 --only-show-errors
aws s3 cp ".runtime/exports/$name.tar.gz.sha256" "s3://$bucket/backups/$name.tar.gz.sha256" --region eu-north-1 --sse AES256 --only-show-errors
aws s3 cp ".runtime/exports/$name.digest.json" "s3://$bucket/backups/$name.digest.json" --region eu-north-1 --sse AES256 --only-show-errors
compose run --rm --no-deps -T maintenance find /backups -mindepth 1 -maxdepth 1 -type d -name 'daily-*' -mtime +14 -exec rm -rf -- '{}' +
find .runtime/exports -mindepth 1 -maxdepth 1 -type f -name 'daily-*' -mtime +14 -delete
printf '%s\n' "$name" > /var/lib/qa-job-searcher/last-backup
printf 'Backup uploaded: %s\n' "$name"
