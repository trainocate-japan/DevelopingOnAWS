#!/usr/bin/env bash
#
# seed_data.sh - Create a demo user and load sample meal records.
#
# This is optional. It is handy when you want data already present before a
# demo (so you can show the LSI/GSI queries immediately). It:
#   1. Creates a confirmed Cognito user (fake username, <name>@example.com).
#   2. Sets a permanent password.
#   3. Writes several meal items straight into DynamoDB using the user's "sub".
#
# Usage:
#   ./seed_data.sh
#
# Environment overrides (must match deploy.sh):
#   AWS_PROFILE   (未指定なら既定プロファイル)
#   AWS_REGION    (default: us-east-1)
#   PROJECT_NAME  (default: meallog)
#   STACK_NAME    (default: meallog-demo)
#   SEED_USERNAME (default: demofan100)
#   SEED_PASSWORD (default: Demo100log!)
#
set -euo pipefail

AWS_PROFILE="${AWS_PROFILE:-}"
AWS_REGION="${AWS_REGION:-us-east-1}"
PROJECT_NAME="${PROJECT_NAME:-meallog}"
STACK_NAME="${STACK_NAME:-meallog-demo}"
SEED_USERNAME="${SEED_USERNAME:-demofan100}"
SEED_PASSWORD="${SEED_PASSWORD:-Demo100log!}"
SEED_EMAIL="${SEED_USERNAME}@example.com"

AWS=(aws --region "${AWS_REGION}")
[ -n "${AWS_PROFILE}" ] && AWS+=(--profile "${AWS_PROFILE}")

get_output() {
  "${AWS[@]}" cloudformation describe-stacks --stack-name "${STACK_NAME}" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

USER_POOL_ID="$(get_output UserPoolId)"
TABLE_NAME="$(get_output MealsTableName)"

echo ">> Creating demo user '${SEED_USERNAME}' in pool ${USER_POOL_ID}"
"${AWS[@]}" cognito-idp admin-create-user \
  --user-pool-id "${USER_POOL_ID}" \
  --username "${SEED_USERNAME}" \
  --user-attributes Name=email,Value="${SEED_EMAIL}" Name=email_verified,Value=true Name=nickname,Value="${SEED_USERNAME}" \
  --message-action SUPPRESS >/dev/null 2>&1 || echo "   (user may already exist, continuing)"

"${AWS[@]}" cognito-idp admin-set-user-password \
  --user-pool-id "${USER_POOL_ID}" \
  --username "${SEED_USERNAME}" \
  --password "${SEED_PASSWORD}" \
  --permanent >/dev/null

# Resolve the user's "sub" (used as the DynamoDB partition key / UserId).
USER_SUB="$("${AWS[@]}" cognito-idp admin-get-user \
  --user-pool-id "${USER_POOL_ID}" \
  --username "${SEED_USERNAME}" \
  --query "UserAttributes[?Name=='sub'].Value" --output text)"
echo "   user sub: ${USER_SUB}"

# Fixed, global favorites partition key (sparse GSI shared across all users).
FAV_FLAG="fav"

# Slot name -> number used in the composite sort key "<date>#<1|2|3>".
slot_num() {
  case "$1" in
    breakfast) echo 1 ;;
    lunch)     echo 2 ;;
    dinner)    echo 3 ;;
    *)         echo 0 ;;
  esac
}

put_meal() {
  local date="$1" slot="$2" menu="$3" rating="$4" favorite="$5"
  local key="${date}#$(slot_num "${slot}")"
  local item
  if [ "${favorite}" = "true" ]; then
    # Favorite item: include FavoriteFlag so it appears in the sparse GSI.
    item=$(cat <<JSON
{
  "UserId": {"S": "${USER_SUB}"},
  "MealKey": {"S": "${key}"},
  "MealDate": {"S": "${date}"},
  "Slot": {"S": "${slot}"},
  "Menu": {"S": "${menu}"},
  "Rating": {"N": "${rating}"},
  "Favorite": {"BOOL": true},
  "Owner": {"S": "${SEED_USERNAME}"},
  "FavoriteFlag": {"S": "${FAV_FLAG}"}
}
JSON
)
  else
    item=$(cat <<JSON
{
  "UserId": {"S": "${USER_SUB}"},
  "MealKey": {"S": "${key}"},
  "MealDate": {"S": "${date}"},
  "Slot": {"S": "${slot}"},
  "Menu": {"S": "${menu}"},
  "Rating": {"N": "${rating}"},
  "Favorite": {"BOOL": false},
  "Owner": {"S": "${SEED_USERNAME}"}
}
JSON
)
  fi
  "${AWS[@]}" dynamodb put-item --table-name "${TABLE_NAME}" --item "${item}" >/dev/null
  echo "   + ${key}  ${menu} (★${rating}${favorite:+, fav=${favorite}})"
}

# Remove any existing records for this sample user first (so re-running the
# script is idempotent and old key formats do not linger).
echo ">> Clearing existing sample data for ${SEED_USERNAME}"
"${AWS[@]}" dynamodb query --table-name "${TABLE_NAME}" \
  --key-condition-expression "UserId = :u" \
  --expression-attribute-values "{\":u\":{\"S\":\"${USER_SUB}\"}}" \
  --query "Items[].MealKey.S" --output text 2>/dev/null | tr '\t' '\n' | while read -r k; do
    [ -n "${k}" ] && "${AWS[@]}" dynamodb delete-item --table-name "${TABLE_NAME}" \
      --key "{\"UserId\":{\"S\":\"${USER_SUB}\"},\"MealKey\":{\"S\":\"${k}\"}}"
  done

echo ">> Loading sample meals into ${TABLE_NAME}"
# デモの鮮度を保つため、日付は「構築当日の約1か月前〜当日」で動的に生成する。
# 各日の朝/昼/夜について、メニュー・評価・お気に入りをローテーションで割り当てる。
# 日付計算は OS 差を避けるため Python で行う（GNU/BSD date 非依存）。
DAYS_BACK="${SEED_DAYS_BACK:-30}"

# メニュー候補（朝/昼/夜）
B_MENUS=("トーストと目玉焼き" "ヨーグルトとフルーツ" "おにぎりと味噌汁" "シリアルと牛乳" "フレンチトースト" "バナナとパン" "和定食")
L_MENUS=("鶏の照り焼き弁当" "海鮮丼" "ラーメン" "カルボナーラ" "きつねうどん" "サラダチキンプレート" "そば")
D_MENUS=("湯豆腐と焼き魚" "野菜炒め定食" "焼肉" "麻婆豆腐定食" "鍋焼きうどん" "牛丼" "ハンバーグ")

# 当日から過去 DAYS_BACK 日分の日付（YYYY-MM-DD）を新しい順に取得。
DATES=$(python3 -c "
import datetime
base = datetime.date.today()
for i in range($DAYS_BACK, -1, -1):
    print((base - datetime.timedelta(days=i)).isoformat())
")

i=0
for d in ${DATES}; do
  # 配列から順番に選ぶ（添字は曜日的にローテーション）。
  b="${B_MENUS[$((i % ${#B_MENUS[@]}))]}"
  l="${L_MENUS[$((i % ${#L_MENUS[@]}))]}"
  dn="${D_MENUS[$((i % ${#D_MENUS[@]}))]}"
  # 評価は 3〜5 を回し、お気に入りは数日おきに付ける。
  br=$(( (i % 3) + 3 ))
  lr=$(( ((i + 1) % 3) + 3 ))
  drt=$(( ((i + 2) % 3) + 3 ))
  bf="false"; lf="false"; df="false"
  [ $((i % 4)) -eq 0 ] && df="true"
  [ $((i % 5)) -eq 0 ] && lf="true"
  put_meal "$d" "breakfast" "$b"  "$br"  "$bf"
  put_meal "$d" "lunch"     "$l"  "$lr"  "$lf"
  put_meal "$d" "dinner"    "$dn" "$drt" "$df"
  i=$((i + 1))
done

echo ""
echo "=============================================="
echo " Seed complete."
echo "   Sign in with:"
echo "     username: ${SEED_USERNAME}"
echo "     password: ${SEED_PASSWORD}"
echo "=============================================="
