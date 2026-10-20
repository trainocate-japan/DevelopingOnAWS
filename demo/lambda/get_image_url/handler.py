"""
処理済み画像（Web 用 / サムネイル）の参照用 署名付き URL を発行する。
GET /meals/{mealKey}/image-url[?userId=<sub>]

出力バケットは非公開なので、ログインしていないと画像を直接参照できない。
サインイン済みのユーザーに対して、都度 期限付きの GET URL を発行する。
対象の画像キーは DynamoDB 項目（ImageWebKey / ImageThumbKey）から取得する。
"""
import os
from urllib.parse import unquote

import boto3

from common import (
    ApiError,
    TABLE_NAME,
    dynamodb,
    from_item,
    get_user_id,
    logger,
    response,
    target_user_id,
)

s3 = boto3.client("s3")
OUTPUT_BUCKET = os.environ["OUTPUT_BUCKET"]
URL_TTL = int(os.environ.get("URL_TTL", "300"))


def _presign(key):
    if not key:
        return None
    return s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": OUTPUT_BUCKET, "Key": key},
        ExpiresIn=URL_TTL,
    )


def lambda_handler(event, context):
    logger.info("get_image_url invoked")
    try:
        if event.get("httpMethod") == "OPTIONS":
            return response(200, {})
        # 有効なトークンは必須。対象ユーザーは userId で切替え可能。
        get_user_id(event)
        user_id = target_user_id(event)

        params = event.get("pathParameters") or {}
        meal_key = params.get("mealKey")
        if not meal_key:
            raise ApiError(400, "mealKey path parameter is required")
        meal_key = unquote(meal_key)

        result = dynamodb.get_item(
            TableName=TABLE_NAME,
            Key={"UserId": {"S": user_id}, "MealKey": {"S": meal_key}},
        )
        item = result.get("Item")
        if not item:
            raise ApiError(404, "Meal record not found")
        data = from_item(item)

        web_url = _presign(data.get("ImageWebKey"))
        thumb_url = _presign(data.get("ImageThumbKey"))
        logger.info("issued image URLs for %s/%s (web=%s)", user_id, meal_key, bool(web_url))
        return response(200, {
            "mealKey": meal_key,
            "webUrl": web_url,
            "thumbUrl": thumb_url,
            "description": data.get("Description"),
            "imageStatus": data.get("ImageStatus"),
        })

    except ApiError as err:
        logger.warning("get_image_url client error %s: %s", err.status, err.message)
        return response(err.status, {"error": err.message})
    except Exception:
        logger.exception("get_image_url unhandled error")
        return response(500, {"error": "Internal server error"})
