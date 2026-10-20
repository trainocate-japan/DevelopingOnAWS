"""
ユーザーの食事記録の一覧（ソートキーの範囲検索つき）。  GET /meals

クエリパラメータ（すべて任意）:
  userId    : どのユーザーの記録を読むか（既定: サインイン中のユーザー）
  dateFrom  : YYYY-MM-DD  -> MealKey >= "<dateFrom>#1"
  dateTo    : YYYY-MM-DD  -> MealKey <= "<dateTo>#3"
  date      : YYYY-MM-DD  -> 単一日に限定（slotFrom/slotTo と併用）
  slotFrom  : 1|2|3（朝/昼/夜）その日の時間帯の下限
  slotTo    : 1|2|3 その日の時間帯の上限

すべて KeyConditionExpression だけで表現し（フィルタ式は使わない）、
効率的な Query になるようにしている:
  - 日付指定なし              -> UserId = :u
  - dateFrom と dateTo         -> MealKey BETWEEN "<dateFrom>#1" AND "<dateTo>#3"
  - dateFrom のみ              -> MealKey >= "<dateFrom>#1"
  - dateTo のみ                -> MealKey <= "<dateTo>#3"
  - date（+ 任意の時間帯範囲）  -> MealKey BETWEEN "<date>#<slotFrom>" AND "<date>#<slotTo>"
"""
from common import (
    ApiError,
    SLOT_MAX,
    SLOT_MIN,
    TABLE_NAME,
    dynamodb,
    from_item,
    logger,
    response,
    target_user_id,
)


def _slot_bound(params, name, default):
    """時間帯の境界値（1〜3）を取り出す。未指定なら既定値。"""
    raw = params.get(name)
    if raw is None or raw == "":
        return default
    try:
        val = int(raw)
    except ValueError:
        raise ApiError(400, f"'{name}' must be 1, 2 or 3")
    if not SLOT_MIN <= val <= SLOT_MAX:
        raise ApiError(400, f"'{name}' must be between {SLOT_MIN} and {SLOT_MAX}")
    return val


def build_query(user_id, params):
    """
    KeyConditionExpression の文字列と ExpressionAttributeValues を組み立てる。
    (式, 属性値, 説明) を返す。
    """
    date = (params.get("date") or "").strip()
    date_from = (params.get("dateFrom") or "").strip()
    date_to = (params.get("dateTo") or "").strip()

    values = {":u": {"S": user_id}}

    # 単一日の指定（時間帯の範囲は任意: 朝〜夜）
    if date:
        slot_from = _slot_bound(params, "slotFrom", SLOT_MIN)
        slot_to = _slot_bound(params, "slotTo", SLOT_MAX)
        if slot_from > slot_to:
            raise ApiError(400, "'slotFrom' must be <= 'slotTo'")
        values[":from"] = {"S": f"{date}#{slot_from}"}
        values[":to"] = {"S": f"{date}#{slot_to}"}
        expr = "UserId = :u AND MealKey BETWEEN :from AND :to"
        return expr, values, f"date={date} slots {slot_from}-{slot_to}"

    # 日付範囲の指定（その日の全時間帯 1〜3 をカバー）
    if date_from and date_to:
        values[":from"] = {"S": f"{date_from}#{SLOT_MIN}"}
        values[":to"] = {"S": f"{date_to}#{SLOT_MAX}"}
        return "UserId = :u AND MealKey BETWEEN :from AND :to", values, f"date {date_from}..{date_to}"
    if date_from:
        values[":from"] = {"S": f"{date_from}#{SLOT_MIN}"}
        return "UserId = :u AND MealKey >= :from", values, f"date >= {date_from}"
    if date_to:
        values[":to"] = {"S": f"{date_to}#{SLOT_MAX}"}
        return "UserId = :u AND MealKey <= :to", values, f"date <= {date_to}"

    return "UserId = :u", values, "all"


def lambda_handler(event, context):
    logger.info("list_recent invoked")
    try:
        if event.get("httpMethod") == "OPTIONS":
            return response(200, {})
        user_id = target_user_id(event)
        params = event.get("queryStringParameters") or {}

        expr, values, desc = build_query(user_id, params)
        result = dynamodb.query(
            TableName=TABLE_NAME,
            KeyConditionExpression=expr,
            ExpressionAttributeValues=values,
            ScanIndexForward=False,  # 新しい順
        )
        items = [from_item(it) for it in result.get("Items", [])]
        logger.info("list_recent (%s) returned %d items for user %s", desc, len(items), user_id)
        return response(200, {"mode": "recent", "userId": user_id, "range": desc, "count": len(items), "items": items})

    except ApiError as err:
        logger.warning("list_recent client error %s: %s", err.status, err.message)
        return response(err.status, {"error": err.message})
    except Exception:
        logger.exception("list_recent unhandled error")
        return response(500, {"error": "Internal server error"})
