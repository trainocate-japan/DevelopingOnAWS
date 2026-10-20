"""
画像処理 Lambda（Lambda A）。SQS(ESM) トリガー。

アップロードバケットの ObjectCreated イベントを、SNS -> SQS(RAW) 経由で受け取り、
Pillow で「Web 表示用」と「サムネイル用」の画像を生成して出力バケットへ保存する。
生成したオブジェクトキーと状態を DynamoDB の該当項目へ書き戻す。
Pillow は専用レイヤーで提供する。
"""
import io
import json
import os

import boto3
from PIL import Image

from common import (
    TABLE_NAME,
    dynamodb,
    logger,
    meal_key_from_object_key,
)

s3 = boto3.client("s3")
OUTPUT_BUCKET = os.environ["OUTPUT_BUCKET"]

WEB_MAX = (1200, 1200)   # Web 表示用の最大サイズ
THUMB_MAX = (300, 300)   # サムネイル用の最大サイズ


def _resize_to_jpeg(raw_bytes, max_size):
    """バイト列の画像を縮小し、JPEG のバイト列で返す。"""
    img = Image.open(io.BytesIO(raw_bytes))
    img = img.convert("RGB")  # PNG 等でも JPEG 保存できるように
    img.thumbnail(max_size)   # アスペクト比を保ったまま縮小
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _process_one(bucket, object_key):
    logger.info("processing image s3://%s/%s", bucket, object_key)

    # 元画像を取得
    obj = s3.get_object(Bucket=bucket, Key=object_key)
    raw = obj["Body"].read()

    # DynamoDB のキーを復元
    user_id, meal_key = meal_key_from_object_key(object_key)

    # 出力キー（拡張子は jpg 固定）
    base = object_key.rsplit(".", 1)[0]
    web_key = f"{base}_web.jpg"
    thumb_key = f"{base}_thumb.jpg"

    # 生成して保存
    s3.put_object(Bucket=OUTPUT_BUCKET, Key=web_key,
                  Body=_resize_to_jpeg(raw, WEB_MAX), ContentType="image/jpeg")
    s3.put_object(Bucket=OUTPUT_BUCKET, Key=thumb_key,
                  Body=_resize_to_jpeg(raw, THUMB_MAX), ContentType="image/jpeg")

    # DynamoDB に書き戻す（画像キーと状態）
    dynamodb.update_item(
        TableName=TABLE_NAME,
        Key={"UserId": {"S": user_id}, "MealKey": {"S": meal_key}},
        UpdateExpression="SET ImageWebKey = :w, ImageThumbKey = :t, ImageStatus = :s, ImageSourceKey = :o",
        ExpressionAttributeValues={
            ":w": {"S": web_key},
            ":t": {"S": thumb_key},
            ":s": {"S": "processed"},
            ":o": {"S": object_key},
        },
    )
    logger.info("saved web/thumb and updated item %s/%s", user_id, meal_key)
    return {"userId": user_id, "mealKey": meal_key, "webKey": web_key, "thumbKey": thumb_key}


def _extract_records(sqs_body):
    """SQS メッセージ本文（RAW 配信の S3 イベント）から S3 レコードを取り出す。"""
    msg = json.loads(sqs_body)
    # RAW メッセージ配信なので S3 イベントがそのまま入っている。
    return msg.get("Records", [])


def lambda_handler(event, context):
    logger.info("process_image invoked: %d SQS record(s)", len(event.get("Records", [])))
    results = []
    for sqs_record in event.get("Records", []):
        for s3_record in _extract_records(sqs_record["body"]):
            bucket = s3_record["s3"]["bucket"]["name"]
            key = s3_record["s3"]["object"]["key"]
            results.append(_process_one(bucket, key))
        return {"processed": results}
