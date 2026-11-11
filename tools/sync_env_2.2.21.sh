#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# همگام‌سازی .env با موتور جدید نسخهٔ ۲.۲.۲۱
# (سفارش با هر تعداد اکانت کامل اجرا می‌شود؛ تنها محدودیت = ۵ سفارش هم‌زمان)
#
#   bash tools/sync_env_2.2.21.sh            # اعمال تغییرات (با پشتیبان‌گیری)
#   bash tools/sync_env_2.2.21.sh --dry-run   # فقط نمایش تغییرات
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ENV_FILE="${ENV_FILE:-.env}"
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

if [ ! -f "$ENV_FILE" ]; then
  echo "❌ فایل $ENV_FILE پیدا نشد. از ریشهٔ پروژه اجرا کنید یا ENV_FILE را ست کنید."
  exit 1
fi

# کلیدهایی که باید مقدار جدید داشته باشند (قبلی → جدید)
declare -A WANTED=(
  [MAX_ACTIVE_ORDERS]=5
  [VOICE_JOIN_INITIAL_CONCURRENCY]=10
  [VOICE_JOIN_MAX_CONCURRENCY]=30
  [GLOBAL_JOIN_CONCURRENCY]=64
  [CLIENT_CREATE_CONCURRENCY]=24
  [VOICE_ACCOUNT_ATTEMPT_LIMIT]=0
  [VOICE_JOIN_START_STAGGER_MIN]=0.5
  [VOICE_JOIN_START_STAGGER_MAX]=1.0
  [VOICE_JOIN_START_JITTER_MIN]=0.0
  [VOICE_JOIN_START_JITTER_MAX]=0.0
  [VOICE_REFILL_RETRY_SECONDS]=90
  [VOICE_DROP_DEDUPE_SECONDS]=120
)

if [ "$DRY_RUN" = "0" ]; then
  BACKUP="$ENV_FILE.bak.$(date +%F-%H%M%S)"
  cp "$ENV_FILE" "$BACKUP"
  echo "🗄  پشتیبان: $BACKUP"
fi

CHANGED=0
for KEY in "${!WANTED[@]}"; do
  VAL="${WANTED[$KEY]}"
  OLD="$(grep -E "^${KEY}=" "$ENV_FILE" | tail -n1 | cut -d= -f2- || true)"
  if [ -n "$OLD" ] && [ "$OLD" = "$VAL" ]; then
    continue
  fi
  if [ -n "$OLD" ]; then
    echo "♻️  $KEY: $OLD → $VAL"
    [ "$DRY_RUN" = "0" ] && sed -i -E "s|^${KEY}=.*|${KEY}=${VAL}|" "$ENV_FILE"
  else
    echo "➕ $KEY=$VAL"
    [ "$DRY_RUN" = "0" ] && printf '%s=%s\n' "$KEY" "$VAL" >> "$ENV_FILE"
  fi
  CHANGED=$((CHANGED + 1))
done

# کلیدهای تکراری (خطوط قدیمی‌تر همان کلید) حذف می‌شوند تا مقدار آخر برنده نباشد
if [ "$DRY_RUN" = "0" ]; then
  for KEY in "${!WANTED[@]}"; do
    COUNT="$(grep -cE "^${KEY}=" "$ENV_FILE" || true)"
    if [ "$COUNT" -gt 1 ]; then
      # آخرین خط را نگه می‌داریم؛ خطوط قبلی حذف می‌شوند
      LAST="$(grep -nE "^${KEY}=" "$ENV_FILE" | tail -n1 | cut -d: -f1)"
      awk -v key="$KEY" -v last="$LAST" 'BEGIN{FS="="} !(index($0, key"=")==1 && NR!=last)' \
        "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
      echo "🧹 خطوط تکراری $KEY حذف شد (تعداد: $COUNT)"
    fi
  done
fi

if [ "$CHANGED" = "0" ]; then
  echo "✅ همهٔ کلیدها از قبل درست بودند؛ تغییری لازم نبود."
else
  echo "✅ $CHANGED کلید به‌روزرسانی شد."
fi
echo
echo "مرحلهٔ بعد: docker compose up -d --build --no-deps bot"
