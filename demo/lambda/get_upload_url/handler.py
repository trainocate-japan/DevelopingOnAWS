"""
画像アップロード用の署名付き URL を発行する。  POST /meals/{mealKey}/image-url

ブラウザはこの URL に対して直接 S3 へ PUT する（Lambda を経由しない）。
オブジェクトキーは DynamoDB のプライマリキー（UserId / MealKey）が分かる形にし、
整理しやすいよう先頭に 年/月 のプレフィックスを付ける。
"""
import json
import os
from urllib.parse import unquote

import boto3

from common import (
    ApiError,
    get_user_id,
    image_object_key,
    logger,
    response,
)

s3 = boto3.client("s3")
UPLOAD_BUCKET = os.environ["UPLOAD_BUCKET"]
URL_TTL = int(os.environ.get("URL_TTL", "300"))

# 受け付ける拡張子と Content-Type の対応。
CONTENT_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}


def lambda_handler(event, context):
    logger.info("get_upload_url invoked")
    try:
        if event.get("httpMethod") == "OPTIONS":
            return response(200, {})
        user_id = get_user_id(event)

        params = event.get("pathParameters") or {}
        meal_key = params.get("mealKey")
        if not meal_key:
            raise ApiError(400, "mealKey path parameter is required")
        meal_key = unquote(meal_key)

        body = json.loads(event.get("body") or "{}")
        ext = (body.get("ext") or "jpg").lower().lstrip(".")
        if ext not in CONTENT_TYPES:
            raise ApiError(400, f"ext must be one of {list(CONTENT_TYPES)}")

        object_key = image_object_key(user_id, meal_key, ext)
        content_type = CONTENT_TYPES[ext]

        # S3 への直接 PUT 用の署名付き URL を発行する。
        url = s3.generate_presigned_url(
            ClientMethod="put_object",
            Params={
                "Bucket": UPLOAD_BUCKET,
                "Key": object_key,
                "ContentType": content_type,
            },
            ExpiresIn=URL_TTL,
        )
        logger.info("issued upload URL for %s", object_key)
        return response(200, {"uploadUrl": url, "objectKey": object_key, "contentType": content_type})

    except ApiError as err:
        logger.warning("get_upload_url client error %s: %s", err.status, err.message)
        return response(err.status, {"error": err.message})
    except Exception:
        logger.exception("get_upload_url unhandled error")
        return response(500, {"error": "Internal server error"})
