"""
MealLog デモ用の Cognito トリガー Lambda。

1 つの関数で 2 つのトリガーを処理し、新規ユーザーがメールのやり取りや
個人情報の入力なしにサインアップ→サインインできるようにする:

  PreSignUp (PreSignUp_SignUp)
      ユーザーを自動確認し、メールを自動検証してアカウントを即座に有効化する。
      これにより、確認コードを受け取ったり入力したりせずにサインアップ→
      サインインできる。

  PostConfirmation (PostConfirmation_ConfirmSignUp)
      アカウントが有効化された直後に実行される「アクティベート後処理」のフック。
      デモではアクティベートをログに記録し、表示名（nickname 属性）が
      確実に存在するようにする。

Cognito トリガーは（必要に応じて変更した）event オブジェクトを必ず返す。
ログのフォーマット/レベル（JSON・INFO）は Lambda 関数側の LoggingConfig で
制御する（CloudFormation テンプレートを参照）。
"""
import json
import logging

import boto3

logger = logging.getLogger()

idp = boto3.client("cognito-idp")


def handle_pre_sign_up(event):
    # メール/電話の検証をスキップし、ユーザーを自動確認する。
    event["response"]["autoConfirmUser"] = True
    if "email" in event["request"].get("userAttributes", {}):
        event["response"]["autoVerifyEmail"] = True
    logger.info("PreSignUp: auto-confirming user '%s'", event.get("userName"))
    return event


def handle_post_confirmation(event):
    user_pool_id = event["userPoolId"]
    username = event["userName"]
    attributes = event["request"].get("userAttributes", {})
    logger.info("PostConfirmation: user '%s' activated in pool %s", username, user_pool_id)

    # フロントが常に表示名を持てるよう、nickname が無ければ補完する。
    if not attributes.get("nickname"):
        try:
            idp.admin_update_user_attributes(
                UserPoolId=user_pool_id,
                Username=username,
                UserAttributes=[{"Name": "nickname", "Value": username}],
            )
            logger.info("PostConfirmation: set nickname for '%s'", username)
        except Exception:  # noqa: BLE001 - アクティベートは絶対に止めない
            logger.exception("PostConfirmation: could not set nickname for '%s'", username)
    return event


def lambda_handler(event, context):
    trigger = event.get("triggerSource", "")
    logger.info("Cognito trigger '%s': %s", trigger, json.dumps(event))

    if trigger == "PreSignUp_SignUp":
        return handle_pre_sign_up(event)
    if trigger == "PostConfirmation_ConfirmSignUp":
        return handle_post_confirmation(event)

    # それ以外のトリガー（管理者作成やパスワード忘れなど）はそのまま通す。
    return event
