#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# همگام‌سازی .env با ضد بن نسخهٔ ۲.۲.۲۳
# (خروج یک‌هفته‌ای یکی‌یکی + قفل ۲۰ دقیقه‌ای بعد از لغو سفارش)
#
#   bash tools/sync_env_2.2.23.sh            # اعمال تغییرات (با پشتیبان‌گیری)
#   bash tools/sync_env_2.2.23.sh --dry-run   # فقط نمایش تغییرات
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ENV_FILE="${ENV_FILE:-.env}"
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

if [ ! -f "$ENV_FILE" ]; then
  echo "❌ فایل $ENV_FILE پیدا نشد. از ریشهٔ پروژه اجرا کنید یا ENV_FILE را ست کنید."
  exit 1
fi

declare -A WANTED=(
  [GROUP_LEAVE_DELAY_MINUTES]=10080
  [GROUP_LEAVE_POLL_MINUTES]=10
  [GROUP_LEAVE_BATCH_LIMIT]=6
  [GROUP_LEAVE_STAGGER_MIN]=60
  [GROUP_LEAVE_STAGGER_MAX]=180
  [GROUP_LEAVE_MAX_CONCURRENCY]=1
  [CANCEL_ORDER_COOLDOWN_MINUTES]=20
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

if [ "$DRY_RUN" = "0" ]; then
  for KEY in "${!WANTED[@]}"; do
    COUNT="$(grep -cE "^${KEY}=" "$ENV_FILE" || true)"
    if [ "$COUNT" -gt 1 ]; then
      TMP="$(mktemp)"
      awk -v k="$KEY" '
        $0 ~ "^"k"=" { last=$0; next }
        { print }
        END { if (last != "") print last }
      ' "$ENV_FILE" > "$TMP"
      mv "$TMP" "$ENV_FILE"
    fi
  done
fi

if [ "$CHANGED" -eq 0 ]; then
  echo "✅ $ENV_FILE از قبل با ۲.۲.۲۳ هم‌خوان است."
else
  echo "✅ $CHANGED کلید به‌روز شد."
fi
