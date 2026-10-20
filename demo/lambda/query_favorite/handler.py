"""
全ユーザーのお気に入り一覧（日付範囲の指定が可能）。  GET /meals/favorites

スパース GSI "FavoriteIndex"（PK=FavoriteFlag="fav", SK=MealDate）を Query する。
FavoriteFlag はお気に入り項目にだけ書き込む単一固定値なので、このインデックスは
スパースかつグローバル。全ユーザーのお気に入りが 1 つのパーティションに集まり、
日付順で一覧できる。

GSI のソートキーは MealDate（YYYY-MM-DD）なので、日付範囲は
KeyConditionExpression だけで表現する（フィルタ式は使わない）:

  dateFrom + dateTo -> MealDate BETWEEN :from AND :to
  dateFrom のみ      -> MealDate >= :from
  dateTo のみ        -> MealDate <= :to
  どちらも無し        -> すべてのお気に入り

サインイン済みなら誰でもこの共有一覧を読める（デモではお気に入りは
全ユーザーに公開される想定）。
"""
from common import (
    ApiError,
    FAVORITE_INDEX,
    TABLE_NAME,
    dynamodb,
    favorite_flag,
    from_item,
    get_user_id,
    logger,
    response,
)


def build_query(params):
    """
    KeyConditionExpression の文字列と ExpressionAttributeValues を組み立てる。
    (式, 属性値, 説明) を返す。
    """
    date_from = (params.get("dateFrom") or "").strip()
    date_to = (params.get("dateTo") or "").strip()

    values = {":f": {"S": favorite_flag()}}

    if date_from and date_to:
        values[":from"] = {"S": date_from}
        values[":to"] = {"S": date_to}
        return "FavoriteFlag = :f AND MealDate BETWEEN :from AND :to", values, f"date {date_from}..{date_to}"
    if date_from:
        values[":from"] = {"S": date_from}
        return "FavoriteFlag = :f AND MealDate >= :from", values, f"date >= {date_from}"
    if date_to:
        values[":to"] = {"S": date_to}
        return "FavoriteFlag = :f AND MealDate <= :to", values, f"date <= {date_to}"
    return "FavoriteFlag = :f", values, "all"


def lambda_handler(event, context):
    logger.info("query_favorite invoked")
    try:
        if event.get("httpMethod") == "OPTIONS":
            return response(200, {})
        # 有効なトークンは必須だが、結果は呼び出しユーザーに限定しない。
        get_user_id(event)

        params = event.get("queryStringParameters") or {}
        expr, values, desc = build_query(params)
        result = dynamodb.query(
            TableName=TABLE_NAME,
            IndexName=FAVORITE_INDEX,
            KeyConditionExpression=expr,
            ExpressionAttributeValues=values,
            ScanIndexForward=False,
        )
        items = [from_item(it) for it in result.get("Items", [])]
        logger.info("query_favorite (%s) returned %d favorite items (all users)", desc, len(items))
        return response(200, {"mode": "favorite", "range": desc, "count": len(items), "items": items})

    except ApiError as err:
        return response(err.status, {"error": err.message})
    except Exception:
        logger.exception("query_favorite unhandled error")
        return response(500, {"error": "Internal server error"})
