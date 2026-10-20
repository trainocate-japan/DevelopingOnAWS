"""
説明生成 Lambda（Lambda B）。SQS(ESM) トリガー。

アップロードバケットの ObjectCreated イベントを SNS -> SQS(RAW) 経由で受け取り、
Bedrock の Nova Lite で料理画像の説明文を生成して DynamoDB へ付与する。
対象項目は S3 オブジェクトキーから復元した (UserId, MealKey) で特定する。

システムプロンプトとモデル ID は Systems Manager パラメータストアから取得する。
"""
import json
import os

import boto3

from common import (
    TABLE_NAME,
    dynamodb,
    logger,
    meal_key_from_object_key,
)

s3 = boto3.client("s3")
ssm = boto3.client("ssm")
bedrock = boto3.client("bedrock-runtime")

SYSTEM_PROMPT_PARAM = os.environ["SYSTEM_PROMPT_PARAM"]
MODEL_ID_PARAM = os.environ["MODEL_ID_PARAM"]

# 拡張子 -> Bedrock へ渡す画像フォーマット名
IMAGE_FORMAT = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}


def _get_param(name):
    return ssm.get_parameter(Name=name)["Parameter"]["Value"]


def _describe(image_bytes, fmt, system_prompt, model_id):
    """Bedrock(Nova) の Converse API で画像の説明文を生成する。"""
    resp = bedrock.converse(
        modelId=model_id,
        system=[{"text": system_prompt}],
        messages=[{
            "role": "user",
            "content": [
                {"image": {"format": fmt, "source": {"bytes": image_bytes}}},
                {"text": "この料理の説明文を作成してください。"},
            ],
        }],
        inferenceConfig={"maxTokens": 300, "temperature": 0.7},
    )
    return resp["output"]["message"]["content"][0]["text"].strip()


def _process_one(bucket, object_key, system_prompt, model_id):
    logger.info("describing image s3://%s/%s", bucket, object_key)

    obj = s3.get_object(Bucket=bucket, Key=object_key)
    raw = obj["Body"].read()
    ext = object_key.rsplit(".", 1)[-1].lower()
    fmt = IMAGE_FORMAT.get(ext, "jpeg")

    description = _describe(raw, fmt, system_prompt, model_id)

    user_id, meal_key = meal_key_from_object_key(object_key)
    dynamodb.update_item(
        TableName=TABLE_NAME,
        Key={"UserId": {"S": user_id}, "MealKey": {"S": meal_key}},
        UpdateExpression="SET Description = :d, DescriptionStatus = :s",
        ExpressionAttributeValues={
            ":d": {"S": description},
            ":s": {"S": "done"},
        },
    )
    logger.info("saved description for %s/%s", user_id, meal_key)
    return {"userId": user_id, "mealKey": meal_key, "description": description}


def _extract_records(sqs_body):
    msg = json.loads(sqs_body)
    return msg.get("Records", [])


def lambda_handler(event, context):
    logger.info("describe_image invoked: %d SQS record(s)", len(event.get("Records", [])))
    # パラメータは呼び出しごとに 1 回だけ取得する。
    system_prompt = _get_param(SYSTEM_PROMPT_PARAM)
    model_id = _get_param(MODEL_ID_PARAM)

    results = []
    for sqs_record in event.get("Records", []):
        for s3_record in _extract_records(sqs_record["body"]):
            bucket = s3_record["s3"]["bucket"]["name"]
            key = s3_record["s3"]["object"]["key"]
            results.append(_process_one(bucket, key, system_prompt, model_id))
    return {"described": results}
