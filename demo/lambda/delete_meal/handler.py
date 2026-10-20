"""
食事記録の削除。  DELETE /meals/{mealKey}

条件付き削除を使い、存在しないキーには 404 を返す（成功扱いにしない）。
コースで扱う DynamoDB の条件付き書き込みのデモにもなっている。
"""
from urllib.parse import unquote

from common import ApiError, TABLE_NAME, dynamodb, get_user_id, logger, response, to_item


def lambda_handler(event, context):
    logger.info("delete_meal invoked")
    try:
        if event.get("httpMethod") == "OPTIONS":
            return response(200, {})
        user_id = get_user_id(event)

        params = event.get("pathParameters") or {}
        meal_key = params.get("mealKey")
        if not meal_key:
            raise ApiError(400, "mealKey path parameter is required")
        # API Gateway はパスパラメータを URL エンコードのまま渡す
        # （"#" 区切りは "%23" で届く）ので、先にデコードする。
        meal_key = unquote(meal_key)

        try:
            dynamodb.delete_item(
                TableName=TABLE_NAME,
                Key=to_item({"UserId": user_id, "MealKey": meal_key}),
                # 項目が存在するときだけ削除する（条件付きオペレーション）。
                ConditionExpression="attribute_exists(UserId)",
            )
        except dynamodb.exceptions.ConditionalCheckFailedException:
            raise ApiError(404, "Meal record not found")

        logger.info("Deleted meal %s for user %s", meal_key, user_id)
        return response(200, {"message": "deleted", "mealKey": meal_key})

    except ApiError as err:
        logger.warning("delete_meal client error %s: %s", err.status, err.message)
        return response(err.status, {"error": err.message})
    except Exception:
        logger.exception("delete_meal unhandled error")
        return response(500, {"error": "Internal server error"})
