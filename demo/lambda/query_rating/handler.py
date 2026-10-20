"""
評価による検索（範囲対応）。  GET /meals/search/rating

LSI "RatingIndex"（PK=UserId, SK=Rating）を KeyConditionExpression だけで
Query する（フィルタ式は使わない）ので効率的。

クエリパラメータ:
  userId    : どのユーザーの記録を読むか（既定: サインイン中のユーザー）
  rating    : 完全一致           -> Rating = N
  ratingFrom: 下限               -> Rating >= N
  ratingTo  : 上限               -> Rating <= N
  ratingFrom + ratingTo          -> Rating BETWEEN a AND b

`rating`（完全一致）か、`ratingFrom`/`ratingTo`（範囲）のどちらかを指定する。
"""
from common import ApiError, RATING_INDEX, TABLE_NAME, dynamodb, from_item, logger, response, target_user_id


def _rating(params, name):
    """評価値（1〜5）を取り出す。未指定なら None。"""
    raw = params.get(name)
    if raw is None or raw == "":
        return None
    try:
        val = int(raw)
    except ValueError:
        raise ApiError(400, f"'{name}' must be an integer between 1 and 5")
    if not 1 <= val <= 5:
        raise ApiError(400, f"'{name}' must be between 1 and 5")
    return val


def build_query(user_id, params):
    """
    KeyConditionExpression の文字列と ExpressionAttributeValues を組み立てる。
    Rating は DynamoDB の予約語ではないが、可読性のため式属性名 #r を使う。
    (式, 属性名, 属性値, 説明) を返す。
    """
    exact = _rating(params, "rating")
    r_from = _rating(params, "ratingFrom")
    r_to = _rating(params, "ratingTo")

    names = {"#r": "Rating"}
    values = {":u": {"S": user_id}}

    if exact is not None:
        values[":r"] = {"N": str(exact)}
        return "UserId = :u AND #r = :r", names, values, f"= {exact}"
    if r_from is not None and r_to is not None:
        if r_from > r_to:
            raise ApiError(400, "'ratingFrom' must be <= 'ratingTo'")
        values[":from"] = {"N": str(r_from)}
        values[":to"] = {"N": str(r_to)}
        return "UserId = :u AND #r BETWEEN :from AND :to", names, values, f"{r_from}..{r_to}"
    if r_from is not None:
        values[":from"] = {"N": str(r_from)}
        return "UserId = :u AND #r >= :from", names, values, f">= {r_from}"
    if r_to is not None:
        values[":to"] = {"N": str(r_to)}
        return "UserId = :u AND #r <= :to", names, values, f"<= {r_to}"
    raise ApiError(400, "provide 'rating' (exact) or 'ratingFrom'/'ratingTo' (range)")


def lambda_handler(event, context):
    logger.info("query_rating invoked")
    try:
        if event.get("httpMethod") == "OPTIONS":
            return response(200, {})
        user_id = target_user_id(event)
        params = event.get("queryStringParameters") or {}

        expr, names, values, desc = build_query(user_id, params)
        result = dynamodb.query(
            TableName=TABLE_NAME,
            IndexName=RATING_INDEX,
            KeyConditionExpression=expr,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ScanIndexForward=False,
        )
        items = [from_item(it) for it in result.get("Items", [])]
        logger.info("query_rating (Rating %s) returned %d items for user %s", desc, len(items), user_id)
        return response(200, {"mode": "rating", "range": desc, "userId": user_id, "count": len(items), "items": items})

    except ApiError as err:
        logger.warning("query_rating client error %s: %s", err.status, err.message)
        return response(err.status, {"error": err.message})
    except Exception:
        logger.exception("query_rating unhandled error")
        return response(500, {"error": "Internal server error"})
