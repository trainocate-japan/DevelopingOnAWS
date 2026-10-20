# MealLog 食事記録デモ (Developing on AWS)

> ⚠️ **コストに関する注意（自己責任でご利用ください）**
> このデモ環境は AWS 上に実リソース（Lambda / API Gateway / DynamoDB / S3 / CloudFront /
> Cognito / SNS / SQS / WAF / Bedrock など）を作成します。**デプロイしている間や、
> 画像アップロードで Bedrock(Nova) を呼び出すたびに料金が発生します。**
> 料金・セキュリティ・運用のすべてはデプロイする方の責任で管理してください。
> デモが終わったら、必ず [クリーンアップ](#7-クリーンアップ) を実行してリソースを削除してください。
> 本リポジトリの内容は無保証（AS IS）で提供されます。

AWS の主要なサーバーレスサービスを、1 本のアプリとして体験するためのデモ環境です。
利用者が **新規サインアップ → サインイン → 食事データの登録・検索（＋料理写真のアップロード）** まで実際に触れます。

実体は `demo/` 配下にまとまっています（CloudFormation テンプレート・Lambda コード・フロントエンド・各種スクリプト）。

次のような構成を 1 つのアプリで通して確認できます。

```
S3 (ストレージ) → DynamoDB → Lambda → API Gateway → S3 (フロントエンド) + CloudFront → Cognito ユーザープール
```

---

## 1. アプリ概要（テーマ: 健康管理 / 食事記録）

食べたものを記録する健康管理アプリ「MealLog」。1 件の記録は次の属性を持ちます。

| 項目 | 説明 |
|------|------|
| 日付 | `YYYY-MM-DD` |
| 時間帯 | 朝 (breakfast) / 昼 (lunch) / 夜 (dinner) |
| メニュー | 自由入力 |
| 評価 | 星 5 段階 (1〜5) |
| お気に入り | ON/OFF。**OFF のときは属性そのものを付与しない** |

---

## 2. アーキテクチャ

このデモで使用している主な AWS サービスと、その使いどころです。

| サービス | このデモでの使いどころ |
|---|---|
| Amazon S3 | フロントエンド（静的ホスティング）、画像のアップロード／出力バケット |
| Amazon CloudFront | フロントエンド配信（WAF・OAC 付き） |
| Amazon DynamoDB | 食事記録の保存。複合ソートキー・LSI・スパース GSI・条件付き書き込み |
| AWS Lambda | API 処理と非同期の画像処理。バージョン/エイリアス、JSON ログ、ADOT トレース |
| Amazon API Gateway | REST API。prod/test ステージをステージ変数で切替え、Cognito オーソライザーで保護 |
| Amazon Cognito | ユーザープールでサインアップ/サインイン。JWT を API の認可に使用 |
| Amazon SNS / SQS | 画像アップロードイベントのファンアウトと非同期処理（DLQ 付き） |
| Amazon Bedrock | 料理画像の説明文を自動生成（Nova Lite） |
| AWS WAF | UI へのレートベースブロック |
| AWS Systems Manager（パラメータストア） | 説明生成のシステムプロンプト／モデル ID の設定 |

### 画像アップロード機能（追加仕様）のアーキテクチャ

```
ブラウザ
  │ ① POST /meals/{mealKey}/image-url  （Cognito 認証）→ get_upload_url Lambda が署名付き PUT URL を発行
  │ ② 署名付き URL で アップロードS3 へ直接 PUT（API Gateway を経由しない）
  ▼
アップロードS3 (meallog-uploads-*)   ← 非公開
  │ ObjectCreated イベント
  ▼
SNS トピック (meallog-image-events)
  ├─(RAW)→ SQS: process-queue  (DLQ: process-dlq)  → process_image Lambda  ※SQS ESM トリガー
  │          Pillow で Web用/サムネ用を生成 → 出力S3(meallog-images-*) → DynamoDB 更新(ImageWebKey/ImageThumbKey/ImageStatus)
  └─(RAW)→ SQS: describe-queue (DLQ: describe-dlq) → describe_image Lambda ※SQS ESM トリガー
             Bedrock Nova Lite で説明生成 → DynamoDB 更新(Description/DescriptionStatus)

表示: ③ GET /meals/{mealKey}/image → get_image_url Lambda が 出力S3 の署名付き GET URL(Web/サムネ)+説明 を返す
      一覧はサムネイル表示、クリックで Web 用画像＋説明のポップアップ
```

- オブジェクトキー: `<年>/<月>/<UserId>/<日付>_<時間帯番号>.<拡張子>`（DynamoDB の PK/SK を復元できる形＋年/月プレフィックス）
- **SSM パラメータストア**: `/meallog/describe/system-prompt`（既定: 150文字でポジティブな感想）、`/meallog/describe/model-id`（既定: `global.amazon.nova-2-lite-v1:0`）
- **画像の参照は署名付き URL のみ**。アップロード用・出力用バケットは両方とも非公開なので、ログインしていないと画像を直接参照できません。
- 画像処理 Lambda は **Pillow を自前レイヤー**（deploy.sh が manylinux 向けにビルド/発行）で利用します。

> 注: SQS の ESM トリガーで動くため、Lambda の「成功/失敗デスティネーション」は使用していません
> （デスティネーションは非同期呼び出し専用で、SQS ESM の同期呼び出しでは発火しないため）。失敗は各キューの DLQ で捕捉します。

### セキュリティ / レート制御

- **Cognito オーソライザー**: データを扱う全 API メソッドは `COGNITO_USER_POOLS` で保護（OPTIONS のみ CORS プリフライト用に NONE）。
- **API Gateway ステージスロットリング**（同時利用 最大 20 名程度を想定）: prod = レート 50 req/s・バースト 100、test = レート 20 req/s・バースト 40。
- **WAF（CloudFront 用 WebACL）**: UI に IP 単位のレートベースブロック。**5 分あたり 2000 リクエスト/IP** を超えるとブロック。

### データモデル（DynamoDB `meallog-meals`）

キー設計は次のとおりです。

- **パーティションキー (PK)**: `UserId` … Cognito の `sub`
- **ソートキー (SK)**: `MealKey` = `"<日付>#<1|2|3>"`（日付 + 時間帯の混合キー。**朝=1 / 昼=2 / 夜=3** と数値化）
  → 時間帯を数値にすることで同一日が朝→昼→夜に正しく並び、ソートキーに対する範囲検索（`>=`/`<=`/`BETWEEN`）が使えます
- **ローカルセカンダリインデックス `RatingIndex`**: PK=`UserId` / SK=`Rating`(数値 1〜5)
  → 「評価で検索」に使用
- **グローバルセカンダリインデックス `FavoriteIndex`（スパース・全ユーザー共通）**: PK=`FavoriteFlag`（固定値 `"fav"`） / SK=`MealDate`
  → `FavoriteFlag` は **お気に入り ON の項目にだけ** 付与。
    OFF の項目には属性を付けないため、インデックスには **お気に入りだけが載る** スパースインデックスになり、
    ストレージと書き込みキャパシティを節約できます。
    値はユーザー非依存の固定値なので、**全ユーザーのお気に入りが 1 つのパーティションに集約** され、
    日付順でまとめて一覧できます（お気に入りはユーザー横断で公開される想定）。

これら 3 つのアクセスパターン（最近の記録 / 評価検索 / お気に入り）を、画面のフィルターから切り替えて確認できます。

- **ソートキーの範囲検索（ベーステーブル）**: `KeyConditionExpression` のみ（フィルタ式なし）で、
  日付範囲（`BETWEEN` / `>=` / `<=`）や、単一日における時間帯の範囲（朝〜昼など）を検索できます。
  日付系パラメータが空のときは **パーティションキー（UserId）のみ** の Query になります。効率を優先し、フィルタ式は使っていません。
- **評価の範囲検索（LSI）**: LSI のソートキー `Rating` に対して、ちょうど（`=`）・以上（`>=`）・以下（`<=`）・
  範囲（`BETWEEN`）を切り替えて検索できます。こちらも `KeyConditionExpression` のみで完結します。
- **お気に入りの日付範囲検索（GSI）**: GSI のソートキー `MealDate` に対して、日付範囲（`BETWEEN` / `>=` / `<=`）で
  絞り込めます。全ユーザーのお気に入りが対象で、こちらも `KeyConditionExpression` のみです。
- **最近の記録 / 評価検索の対象ユーザー切替え**: 既定は自分のデータですが、画面の「対象ユーザー」で
  サンプルユーザー (`demofan100`) に切り替えられます。自分のデータが無い利用者でも検索結果を確認できます
  （API 側は `?userId=<sub>` で対象を指定。デモ用途のため所有者チェックは行いません）。
- **お気に入りは全員共通**: お気に入り一覧は対象ユーザーに関係なく全員分を表示し、各行に登録者名を出します。
  登録フォームのお気に入りチェックボックスには「チェックを入れると他の人にも表示されます」と注意書きを表示します。

### Lambda 関数（機能ごとに分割）

シンプルさ優先で、**1 関数 = 1 操作** にしています。共通処理だけ `common/common.py` にまとめ、デプロイ時に各関数へ同梱します。
コード内のコメント・docstring は日本語で記述しています。

| 関数名 | 役割 | API |
|---|---|---|
| `meallog-put-meal` | 記録の作成/上書き（お気に入り時のみ `FavoriteFlag` を付与） | `POST /meals` |
| `meallog-delete-meal` | 記録の削除（条件付き削除） | `DELETE /meals/{mealKey}` |
| `meallog-list-recent` | 最近の記録一覧（ベーステーブルを Query） | `GET /meals` |
| `meallog-query-rating` | 評価で検索（**LSI** を Query） | `GET /meals/search/rating?rating=N` |
| `meallog-query-favorite` | お気に入り一覧（**スパース GSI** を Query） | `GET /meals/favorites` |
| `meallog-get-upload-url` | 画像アップロード用の署名付き URL 発行 | `POST /meals/{mealKey}/image-url` |
| `meallog-get-image-url` | 画像参照用の署名付き URL 発行（Web/サムネ＋説明） | `GET /meals/{mealKey}/image` |
| `meallog-process-image` | Pillow で Web/サムネ生成・DynamoDB 更新（**SQS ESM**） | SQS process-queue |
| `meallog-describe-image` | Bedrock Nova で説明生成・DynamoDB 更新（**SQS ESM**） | SQS describe-queue |
| `meallog-cognito-triggers` | PreSignUp（自動確認）+ PostConfirmation（アクティベート後処理） | Cognito トリガー |

- **ログ**: 各関数は `logging` でログを出力し、**Lambda の `LoggingConfig`**（`LogFormat: JSON` / `ApplicationLogLevel: INFO`）で
  JSON 形式・INFO レベルに制御します（コード側で `setLevel` はしません）。CloudWatch Logs には
  `{"timestamp":..., "level":"INFO", "message":..., "requestId":...}` の形で出力されます。
- **DynamoDB アクセス**: 低レベルの **`boto3.client("dynamodb")`** を使用し、`query` / `put_item` / `delete_item` / `update_item` / `get_item` を
  `KeyConditionExpression` と型付きの `ExpressionAttributeValues`（`{"S":...}` / `{"N":...}`）で呼び出します。
  属性値の相互変換は `common.py` の `to_item` / `from_item` にまとめています。
- **X-Ray トレース (ADOT)**: 各 Lambda は `TracingConfig: Active` + **ADOT (AWS Distro for OpenTelemetry) Python レイヤー** + `AWS_LAMBDA_EXEC_WRAPPER=/opt/otel-instrument` を設定。
  OpenTelemetry コレクターがトレースを X-Ray へ送信します（ログに `XRAY TraceId: ... Sampled: true` が出力されます）。
- **バージョンとエイリアス**: API 用の 7 関数（meals 系 5 + 画像 API 2）は発行済みバージョンに対して **`prod` / `test` エイリアス** を持ちます。
  SQS ESM で動く 2 関数（process/describe）はエイリアスなしで `$LATEST` を更新します。

### API Gateway のステージ変数による prod/test 切替え

REST API には **`prod` と `test` の 2 ステージ** があり、それぞれステージ変数 `lambdaAlias` を `prod` / `test` に設定しています。
各メソッドの統合 URI は関数 ARN の末尾に `:${stageVariables.lambdaAlias}` を付けているため、

- `…/prod/meals` → 各 Lambda の **`prod` エイリアス** を呼び出し
- `…/test/meals` → 各 Lambda の **`test` エイリアス** を呼び出し

となります。デプロイ時は両エイリアスを同じ新バージョンに向けますが、`test` だけ別バージョンに向ければ
カナリア的な検証にも使えます。

### 認証フロー（個人情報を入力させない）

1. 画面で「架空ユーザーを作成」を押すと、`mealfanXXXX` 形式のユーザー名・パスワードを **自動生成**。
2. メールアドレスは一律 `<ユーザー名>@example.com`、表示名 (nickname) も自動付与。
3. サインアップ時に **PreSignUp Lambda** が `autoConfirmUser=true` / `autoVerifyEmail=true` を返すため、
   メール検証なしで即アクティベート（**PostConfirmation Lambda** も発火）。
4. 作成後、**ログイン情報を画面に表示**。利用者はその情報でサインインします
   （サインアップ → サインインの流れをそのまま体験できます）。

> 自動ログインはせず、**サインアップ → 手動サインイン** を見せる構成にしています
> （新規作成後にログイン情報を画面へ表示）。

---

## 3. 前提

- AWS CLI v2 がインストール済みで、使用する AWS プロファイルで `sts get-caller-identity` が通ること（プロファイルは環境変数 `AWS_PROFILE` で指定）
- `zip`, `python3`（+ `pip`）, `bash`, `curl` が使えること
- リージョンは **`us-east-1`**
- デプロイ先アカウントで CloudFormation / Lambda / DynamoDB / API Gateway / Cognito / CloudFront / S3 / IAM / X-Ray / SNS / SQS / SSM / WAF / Bedrock を作成・利用できる権限
- Bedrock で **Nova Lite（`global.amazon.nova-2-lite-v1:0`）のモデルアクセスが有効化済み**であること

---

## 4. ディレクトリ構成

```
.（リポジトリルート）
├── README.md                 # このファイル
├── demo/
│   ├── cloudformation/
│   │   └── meallog.yaml       # 全リソースの CloudFormation テンプレート
│   ├── lambda/
│   │   ├── common/common.py           # 各 Lambda 共通ヘルパー（ビルド時に同梱）
│   │   ├── put_meal/handler.py        # POST /meals
│   │   ├── delete_meal/handler.py     # DELETE /meals/{mealKey}
│   │   ├── list_recent/handler.py     # GET /meals
│   │   ├── query_rating/handler.py    # GET /meals/search/rating
│   │   ├── query_favorite/handler.py  # GET /meals/favorites
│   │   ├── get_upload_url/handler.py  # POST /meals/{mealKey}/image-url（署名付きPUT URL）
│   │   ├── get_image_url/handler.py   # GET  /meals/{mealKey}/image（署名付きGET URL）
│   │   ├── process_image/handler.py   # SQS ESM: Pillow で web/thumb 生成
│   │   ├── describe_image/handler.py  # SQS ESM: Bedrock Nova で説明生成
│   │   └── cognito_triggers/handler.py# PreSignUp + PostConfirmation
│   ├── frontend/
│   │   ├── index.html
│   │   ├── app.js
│   │   ├── styles.css
│   │   ├── config.sample.js   # config.js の雛形（リポジトリで管理）
│   │   └── config.js          # deploy.sh が自動生成（git 管理外）
│   └── scripts/
│       ├── deploy.sh          # ビルド & デプロイ
│       ├── seed_data.sh       # デモ用ユーザー + サンプルデータ投入（任意）
│       └── cleanup.sh         # 全リソース削除
└── text/                      # コース資料（PPTX）と指示書
```

---

## 5. 構築手順

### 手順 1: デプロイ

リポジトリルートから実行する場合:

```bash
./demo/scripts/deploy.sh
```

`deploy.sh` が行うこと:

1. Lambda 配布用 S3 バケット `meallog-lambda-code-<AccountId>` を作成
2. 各 Lambda を zip 化（API 関数には `common.py` を同梱）して S3 へアップロード
3. Pillow の Lambda レイヤーを manylinux 向けにビルドして発行
4. CloudFormation スタック `meallog-demo` を作成/更新（テンプレートが大きいため S3 経由でアップロード）
5. 各 API 関数のコードを更新 → 新バージョンを発行 → `prod` / `test` エイリアスを付け替え
6. スタック出力から `demo/frontend/config.js` を生成
7. フロントエンドを S3 に同期し、CloudFront キャッシュを無効化

完了すると最後に **App URL**（CloudFront のドメイン）が表示されます。
CloudFront の配信開始まで数分かかることがあります。

プロファイルやリージョンを変えたい場合:

```bash
AWS_PROFILE=別プロファイル AWS_REGION=us-east-1 ./demo/scripts/deploy.sh
```

### 手順 2（任意）: サンプルデータ投入

デモ前にデータを用意しておきたい場合:

```bash
./demo/scripts/seed_data.sh
```

`demofan100` / `Demo100log!` というデモユーザーと、サンプル食事データを投入します。
日付は **構築当日の約 1 か月前〜当日** で動的に生成されるため（朝/昼/夜 × 約 31 日分）、
いつ構築してもデモの鮮度が保たれ、日付範囲検索もそのまま試せます。評価・お気に入りは分散して付与されます。
再実行時は既存のサンプルデータを一度クリアしてから入れ直すので冪等です。

> サンプルユーザーを投入したあとに `deploy.sh` をもう一度実行すると、`config.js` にサンプルユーザーが反映され、
> 画面の「対象ユーザー」ドロップダウンから選べるようになります。

### 手順 3: 動作確認（ブラウザ）

1. App URL を開く
2. 「サインアップ（新規作成）」タブ →「架空ユーザーを作成する」→ 表示されたログイン情報を控える
3. 「サインイン」タブでその情報を入力してサインイン
4. 食事を登録し、下部のフィルター（最近 / 評価で検索 / お気に入り）で DynamoDB の
   ベーステーブル・LSI・GSI それぞれの検索結果を確認
5. 各行の「写真」から料理画像をアップロード。数十秒後に一覧へサムネイルが表示され、
   クリックすると Web 用画像＋自動生成された説明のポップアップが開きます

---

## 6. カスタムドメイン (ACM / Route 53) を後から付ける場合

CloudFront はまずデフォルトドメインで動く状態にしてあります。独自ドメインを使う場合は手動設定する前提です
（以下では `your-domain.example.com` をお使いのドメインに読み替えてください）。

1. **ACM 証明書**を **`us-east-1`** で対象ドメインに対して発行（DNS 検証）
2. `demo/cloudformation/meallog.yaml` の `CloudFrontDistribution` に以下を追加して再デプロイ
   - `DistributionConfig.Aliases: [your-domain.example.com]`
   - `ViewerCertificate` を `CloudFrontDefaultCertificate: true` から
     `AcmCertificateArn` / `SslSupportMethod: sni-only` / `MinimumProtocolVersion: TLSv1.2_2021` に差し替え
3. **Route 53**（または利用中の DNS）で対象ドメインの A/AAAA エイリアスレコードを CloudFront ディストリビューションに向ける

> CloudFront 用の ACM 証明書は **必ず us-east-1** で発行する必要があります（本デモは元々 us-east-1 なので問題ありません）。

---

## 7. クリーンアップ

**デモが終わったら、課金を止めるため必ず実行してください。**

```bash
./demo/scripts/cleanup.sh
```

- フロントエンド / アップロード / 出力の各 S3 バケットを空にする（空でないと削除できないため）
- CloudFormation スタック `meallog-demo` を削除（DynamoDB・Lambda・API Gateway・Cognito・CloudFront・WAF・SNS・SQS・SSM・IAM 等をまとめて削除）
- スタック外で作った Lambda 配布用バケットを空にして削除
- スタック外で発行した Pillow レイヤーの全バージョンを削除

手動でカスタムドメイン（ACM / Route 53）を設定した場合は、それらは別途手動で削除してください。

---

## 8. API リファレンス

すべて Cognito オーソライザーで保護。`Authorization: <ID トークン(JWT)>` ヘッダーが必要です。

| メソッド / パス | 説明 | 例 |
|---|---|---|
| `GET /meals` | 最近の記録 / ソートキー範囲検索（ベーステーブル Query） | 下記参照 |
| `GET /meals/search/rating` | 評価で検索（LSI Query、完全一致 or 範囲） | 下記参照 |
| `GET /meals/favorites` | お気に入り一覧（スパース GSI Query・**全ユーザー共通**・日付範囲可） | 下記参照 |
| `POST /meals` | 記録の作成/上書き | 下記参照 |
| `DELETE /meals/{mealKey}` | 記録の削除（`mealKey` は `日付#時間帯番号`） | `DELETE /prod/meals/2026-10-20%231` |
| `POST /meals/{mealKey}/image-url` | 画像アップロード用の署名付き PUT URL を発行 | body: `{"ext":"jpg"}` |
| `GET /meals/{mealKey}/image` | 画像参照用の署名付き GET URL（Web/サムネ）+説明を返す | `?userId=<sub>` 任意 |

`GET /meals`（ベーステーブル）のクエリパラメータ（すべて任意、`KeyConditionExpression` のみ使用）:

| パラメータ | 意味 |
|---|---|
| `userId` | 対象ユーザーの sub（省略時は自分） |
| `dateFrom` / `dateTo` | 日付範囲。両方 → `BETWEEN`、片方のみ → `>=` / `<=` |
| `date` | 単一日を指定（`slotFrom`/`slotTo` と併用） |
| `slotFrom` / `slotTo` | その日の時間帯範囲（1=朝, 2=昼, 3=夜）。例: 朝〜昼 = `slotFrom=1&slotTo=2` |

（日付系パラメータがすべて空のときは `UserId = :u` のみ、つまりパーティションキーだけで Query します）

例:
- `GET /prod/meals?dateFrom=2026-10-16&dateTo=2026-10-18`（10/16〜10/18）
- `GET /prod/meals?dateFrom=2026-10-19`（10/19 以降）
- `GET /prod/meals?date=2026-10-18&slotFrom=1&slotTo=2`（10/18 の朝〜昼）

`GET /meals/search/rating`（LSI）のクエリパラメータ:

| パラメータ | 意味 |
|---|---|
| `userId` | 対象ユーザーの sub（省略時は自分） |
| `rating` | 完全一致（`= N`） |
| `ratingFrom` | 以上（`>= N`） |
| `ratingTo` | 以下（`<= N`） |
| `ratingFrom` + `ratingTo` | 範囲（`BETWEEN`） |

例:
- `GET /prod/meals/search/rating?rating=5`（★5 ちょうど）
- `GET /prod/meals/search/rating?ratingFrom=4`（★4 以上）
- `GET /prod/meals/search/rating?ratingFrom=3&ratingTo=4`（★3〜★4）

`GET /meals/favorites`（GSI・全ユーザー共通）のクエリパラメータ（任意）:

| パラメータ | 意味 |
|---|---|
| `dateFrom` / `dateTo` | 日付範囲。両方 → `BETWEEN`、片方のみ → `>=` / `<=`。省略時は全件 |

例:
- `GET /prod/meals/favorites`（全お気に入り）
- `GET /prod/meals/favorites?dateFrom=2026-10-16&dateTo=2026-10-18`（期間指定）

`POST /meals` のリクエストボディ:

```json
{
  "date": "2026-10-20",
  "slot": "lunch",
  "menu": "カレーライス",
  "rating": 4,
  "favorite": true
}
```

---

## 9. 動作確認済みの内容 / 注意点

デプロイ後に `us-east-1` で実際に検証した結果:

- ✅ API Lambda が機能ごとに分割され、prod ステージで全エンドポイントが正常動作
- ✅ DynamoDB: 最近の記録、LSI 評価検索、スパース GSI お気に入りが期待どおり
- ✅ ソートキー範囲検索（日付 `BETWEEN`/`>=`/`<=`、単一日の時間帯範囲）がフィルタ式なしで動作
- ✅ 日付系パラメータが空のときは UserId のみ（パーティションキーのみ）で Query
- ✅ LSI 評価の範囲検索（`=`/`>=`/`<=`/`BETWEEN`）がフィルタ式なしで動作
- ✅ GSI お気に入りの日付範囲検索（`BETWEEN`/`>=`/`<=`）が全ユーザー横断・フィルタ式なしで動作
- ✅ 時間帯の数値化により同一日が朝→昼→夜に正しくソートされることを確認
- ✅ お気に入り OFF の項目に `FavoriteFlag` が付かず、GSI に載らないこと（スパース化）を確認
- ✅ お気に入り GSI が **全ユーザー横断**：複数ユーザー分のお気に入りが日付順でまとめて取得でき、各行に登録者名が付くことを確認
- ✅ `?userId=` で別ユーザー（サンプル）の最近の記録・評価検索を閲覧できることを確認
- ✅ 条件付き削除（存在しないキーは 404）、トークンなしは 401
- ✅ API Gateway の **prod / test 両ステージ** が、ステージ変数経由でそれぞれ `prod` / `test` エイリアスを呼び分け
- ✅ 各 Lambda が **バージョン + prod/test エイリアス** を保持
- ✅ ログが Lambda の LoggingConfig で JSON 形式・INFO レベルで出力されること（CloudWatch Logs で確認）
- ✅ DynamoDB アクセスを `boto3.client` の低レベル API に変更後も全エンドポイントが正常動作
- ✅ ADOT コレクターが起動し、ログに `XRAY TraceId: ... Sampled: true` を出力（X-Ray へトレース送信）
- ✅ Cognito トリガー（PreSignUp 自動確認 / PostConfirmation）を検証。ユーザープールは自己サインアップ許可
- ✅ 画像アップロード一連の流れ: 署名付き PUT → S3 → SNS → SQS×2 → process_image が web/thumb 生成（Pillow）→ describe_image が Bedrock Nova で説明生成 → DynamoDB 更新 → get_image_url が署名付き GET URL を返す、を実機で確認
- ✅ サンプルデータの日付が構築当日基準（約 1 か月前〜当日）で動的生成されることを確認
- ✅ WAF WebACL が CloudFront に関連付き、レートベースルール（2000/5分, Block）が設定されていることを確認
- ✅ ステージスロットリング（prod 50/100・test 20/40）を確認

注意点:

- **X-Ray の表示遅延**: コレクターのログ上はトレースが送信されていますが、X-Ray の `GetTraceSummaries` API / コンソールへの反映には数分の遅延があります。すぐに表示されない場合は時間をおいて確認してください。
- **ADOT レイヤー ARN**: `demo/cloudformation/meallog.yaml` の `AdotLayerArn` は us-east-1 / Python / x86_64 用の値です。別リージョンや別アーキテクチャで使う場合は、該当リージョンの ADOT Python レイヤー ARN に差し替えてください。
- **Pillow レイヤー**: deploy.sh が manylinux(amd64/py3.12) 向けに `pip install` してレイヤーを発行します。実行には `pip` が必要です。cleanup.sh がレイヤーの全バージョンを削除します。
- **Bedrock**: `describe_image` は Nova Lite（`global.amazon.nova-2-lite-v1:0`）を呼びます。アカウントで該当モデルのアクセス有効化が必要です。モデル ID / システムプロンプトは SSM パラメータストアで変更できます。
- **Lambda デスティネーション不使用**: 画像処理の 2 関数は SQS ESM（同期呼び出し）で動くため、成功/失敗デスティネーションは設定していません。失敗は各 SQS の DLQ で捕捉します。
- デモ用途のため、CloudFront のキャッシュは無効化、DynamoDB はオンデマンド課金にしています。
