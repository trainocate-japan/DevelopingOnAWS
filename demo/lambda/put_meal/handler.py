"""
食事記録の作成/上書き。  POST /meals

ベーステーブルに項目を書き込む。お気に入りが true のときだけスパース GSI の
パーティションキー（FavoriteFlag）を付与する。false のときは属性自体を付けない
ため、その項目はお気に入りインデックスには載らない。
"""
import json

from common import (
    ApiError,
    TABLE_NAME,
    VALID_SLOTS,
    dynamodb,
    favorite_flag,
    get_display_name,
    get_user_id,
    logger,
    meal_key,
    response,
    to_item,
)


def lambda_handler(event, context):
    logger.info("put_meal invoked")
    try:
        if event.get("httpMethod") == "OPTIONS":
            return response(200, {})
        user_id = get_user_id(event)
        owner = get_display_name(event)

        try:
            payload = json.loads(event.get("body") or "{}")
        except json.JSONDecodeError:
            raise ApiError(400, "Request body must be valid JSON")

        date = (payload.get("date") or "").strip()
        slot = (payload.get("slot") or "").strip().lower()
        menu = (payload.get("menu") or "").strip()
        favorite = bool(payload.get("favorite", False))

        # 入力バリデーション
        if not date:
            raise ApiError(400, "'date' is required (YYYY-MM-DD)")
        if slot not in VALID_SLOTS:
            raise ApiError(400, f"'slot' must be one of {VALID_SLOTS}")
        if not menu:
            raise ApiError(400, "'menu' is required")
        try:
            rating = int(payload.get("rating"))
        except (TypeError, ValueError):
            raise ApiError(400, "'rating' must be an integer between 1 and 5")
        if not 1 <= rating <= 5:
            raise ApiError(400, "'rating' must be between 1 and 5")

        item = {
            "UserId": user_id,
            # ソートキーは数値の時間帯を使う: "<日付>#<1|2|3>"
            "MealKey": meal_key(date, slot),
            "MealDate": date,
            "Slot": slot,
            "Menu": menu,
            "Rating": rating,
            "Favorite": favorite,
            # 登録者の表示名。全ユーザー共通のお気に入り一覧で「誰の記録か」を
            # 表示できるように保持する。
            "Owner": owner,
        }
        if favorite:
            # 固定値（ユーザー非依存）→ グローバルかつスパースなお気に入りインデックス。
            item["FavoriteFlag"] = favorite_flag()

        # 低レベル client の put_item は型付きの項目を受け取る。
        dynamodb.put_item(TableName=TABLE_NAME, Item=to_item(item))
        logger.info("Saved meal %s for user %s (favorite=%s)", item["MealKey"], user_id, favorite)
        return response(201, {"message": "saved", "item": item})

    except ApiError as err:
        logger.warning("put_meal client error %s: %s", err.status, err.message)
        return response(err.status, {"error": err.message})
    except Exception:
        logger.exception("put_meal unhandled error")
        return response(500, {"error": "Internal server error"})
