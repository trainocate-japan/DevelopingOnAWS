"""
MealLog API Lambda 関数の共通ヘルパー。

このモジュールはビルド時に各関数のデプロイパッケージへ同梱される
（scripts/deploy.sh を参照）ため、どの関数からも `import common` で使える。
各ハンドラをシンプルに保つため、共通処理だけをここに集約している。

DynamoDB は低レベルの boto3.client（`boto3.client("dynamodb")`）で操作する。
client API は属性を型付き（{"S": ...} / {"N": ...} など）でやり取りするため、
Python の dict と相互変換するヘルパー（to_item / from_item）も用意している。
"""
import json
import logging
import os

import boto3

# --- ロギング ---------------------------------------------------------------
# ログレベル/フォーマット（JSON・INFO）は Lambda 関数側の LoggingConfig で
# 制御する（CloudFormation テンプレートを参照）。ここではロガーを取得するだけ。
logger = logging.getLogger()

# --- DynamoDB（低レベル client）--------------------------------------------
TABLE_NAME = os.environ["MEALS_TABLE"]
RATING_INDEX = os.environ.get("RATING_INDEX", "RatingIndex")
FAVORITE_INDEX = os.environ.get("FAVORITE_INDEX", "FavoriteIndex")

dynamodb = boto3.client("dynamodb")

VALID_SLOTS = ("breakfast", "lunch", "dinner")

# 時間帯はソートキー内で数値として保持する。こうすることで 1 日が
# 朝 -> 昼 -> 夜 の順に正しく並び、ソートキーの範囲検索（>=, <=, BETWEEN）も
# 使えるようになる。複合ソートキーは "<日付>#<時間帯番号>"（例: "2026-10-18#1"）。
SLOT_NUM = {"breakfast": 1, "lunch": 2, "dinner": 3}
SLOT_FROM_NUM = {1: "breakfast", 2: "lunch", 3: "dinner"}
SLOT_MIN = 1
SLOT_MAX = 3


def meal_key(date, slot):
    """複合ソートキー '<日付>#<時間帯番号>' を組み立てる。"""
    return f"{date}#{SLOT_NUM[slot]}"


def parse_meal_key(key):
    """'<日付>#<時間帯番号>' のソートキーから (日付, 時間帯名) を返す。"""
    date, _, num = (key or "").rpartition("#")
    try:
        return date, SLOT_FROM_NUM.get(int(num), num)
    except ValueError:
        return date, num


def image_object_key(user_id, meal_key_value, ext):
    """
    アップロード画像の S3 オブジェクトキーを組み立てる。
    形式: "<年>/<月>/<UserId>/<日付>_<時間帯番号>.<拡張子>"
    MealKey は "<日付>#<時間帯番号>" なので "#" を "_" に置き換え、
    先頭に年/月のプレフィックスを付けて整理しやすくする。
    """
    date, _, num = meal_key_value.rpartition("#")
    year, month = date.split("-")[0], date.split("-")[1]
    safe = f"{date}_{num}"
    return f"{year}/{month}/{user_id}/{safe}.{ext}"


def meal_key_from_object_key(object_key):
    """
    S3 オブジェクトキーから DynamoDB の (UserId, MealKey) を復元する。
    "<年>/<月>/<UserId>/<日付>_<時間帯番号>.<拡張子>" を分解する。
    """
    parts = object_key.split("/")
    # parts = [year, month, user_id, "<日付>_<時間帯番号>.<拡張子>"]
    user_id = parts[2]
    filename = parts[3]
    stem = filename.rsplit(".", 1)[0]  # "<日付>_<時間帯番号>"
    date, _, num = stem.rpartition("_")
    return user_id, f"{date}#{num}"


# --- DynamoDB の属性値変換 --------------------------------------------------
def to_attr(value):
    """Python の値を DynamoDB の属性値（型付き）に変換する。"""
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float)):
        return {"N": str(value)}
    return {"S": str(value)}


def from_attr(attr):
    """DynamoDB の属性値（型付き）を Python の値に変換する。"""
    if "S" in attr:
        return attr["S"]
    if "N" in attr:
        # 整数に割り切れるなら int、それ以外は float で返す。
        num = float(attr["N"])
        return int(num) if num.is_integer() else num
    if "BOOL" in attr:
        return attr["BOOL"]
    if "NULL" in attr:
        return None
    # 想定外の型はそのまま返す。
    return attr


def to_item(data):
    """Python の dict を DynamoDB 項目（型付き）に変換する。"""
    return {k: to_attr(v) for k, v in data.items()}


def from_item(item):
    """DynamoDB 項目（型付き）を Python の dict に変換する。"""
    return {k: from_attr(v) for k, v in item.items()}


CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
}


class ApiError(Exception):
    """クライアント起因のエラー（HTTP ステータスとメッセージを持つ）。"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def response(status, body):
    """API Gateway (プロキシ統合) 形式のレスポンスを組み立てる。"""
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", **CORS_HEADERS},
        "body": json.dumps(body, ensure_ascii=False),
    }


def _claims(event):
    """Cognito オーソライザーのクレームを取り出す。"""
    try:
        return event["requestContext"]["authorizer"]["claims"]
    except (KeyError, TypeError):
        raise ApiError(401, "Unauthorized: missing Cognito claims")


def get_user_id(event):
    """クレームから Cognito のユーザー ID（sub）を取り出す。"""
    claims = _claims(event)
    try:
        return claims["sub"]
    except (KeyError, TypeError):
        raise ApiError(401, "Unauthorized: missing Cognito claims")


def get_display_name(event):
    """トークンから表示名を取り出す（nickname 優先、無ければユーザー名）。"""
    claims = _claims(event)
    return claims.get("nickname") or claims.get("cognito:username") or "unknown"


def target_user_id(event):
    """
    どのユーザーの記録を読むかを決める。既定は呼び出し元自身の ID だが、
    クエリ文字列 "userId" を指定すると別ユーザー（シード済みのサンプル
    ユーザーなど）のデータを閲覧できる。自分のデータが無い受講者でも
    検索結果を確認できるようにするための仕組み。デモ用途のため所有者
    チェックは行わない。
    """
    own = get_user_id(event)
    params = event.get("queryStringParameters") or {}
    requested = (params.get("userId") or "").strip()
    return requested or own


# GSI のパーティションキー値。ユーザー非依存の単一固定値にすることで、
# FavoriteIndex に全ユーザーのお気に入りが 1 つのパーティションへ集約され、
# 全体の一覧として Query できる。お気に入り項目にだけ付与するため、
# インデックスはスパースになる。
FAVORITE_FLAG_VALUE = "fav"


def favorite_flag(user_id=None):
    """お気に入り項目用の GSI パーティションキー値（全ユーザー共通）。"""
    return FAVORITE_FLAG_VALUE
