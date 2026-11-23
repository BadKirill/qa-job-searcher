#!/usr/bin/env bash
set -euo pipefail

revision="${1:-}"
domain="${2:-}"
allowed_ids="${3:-}"
root="$HOME/qa-job-searcher"
repository="$root/app"

[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || exit 2
[[ "$domain" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}$ ]] || exit 2
[[ -z "$allowed_ids" || "$allowed_ids" =~ ^[0-9]+(,[0-9]+)*$ ]] || exit 2
[[ -s "$root/data/jobs.sqlite3" ]] || exit 3
for name in openai-api-key openai-agent.json ngrok.env; do
  [[ -s "$root/secrets/$name" && -O "$root/secrets/$name" ]] || exit 3
done

if [[ ! -d "$repository/.git" ]]; then
  git clone --quiet --branch master --single-branch https://github.com/BadKirill/qa-job-searcher.git "$repository"
fi
git -C "$repository" fetch --quiet --depth=1 origin master
[[ "$(git -C "$repository" rev-parse FETCH_HEAD)" == "$revision" ]] || exit 4
git -C "$repository" checkout --quiet --force --detach "$revision"

umask 077
printf 'DEPLOY_UID=%s\nDEPLOY_GID=%s\nDEPLOY_DATA_DIR=%s\nDEPLOY_SECRET_DIR=%s\nAPP_DOMAIN=%s\nDEPLOY_REVISION=%s\nTELEGRAM_ALLOWED_USER_IDS=%s\n' \
  "$(id -u)" "$(id -g)" "$root/data" "$root/secrets" "$domain" "$revision" "$allowed_ids" \
  > "$root/runtime.env"

compose=(docker compose -p qa-job-searcher --env-file "$root/runtime.env" -f "$repository/compose.vps.yml")
"${compose[@]}" config -q
if [[ -n "$allowed_ids" ]]; then
  [[ -s "$root/secrets/telegram-bot-token" ]] || exit 3
  "${compose[@]}" --profile bot up -d --build web worker tunnel bot
else
  "${compose[@]}" --profile bot stop bot >/dev/null 2>&1 || true
  "${compose[@]}" up -d --build web worker tunnel
fi
"${compose[@]}" ps
