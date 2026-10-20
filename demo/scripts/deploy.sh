#!/usr/bin/env bash
#
# deploy.sh - Build and deploy the MealLog demo environment.
#
# Steps:
#   1. Create (if needed) an S3 bucket to hold the Lambda deployment packages.
#   2. Zip and upload every Lambda (each API function bundles common.py).
#   3. Deploy / update the CloudFormation stack.
#   4. Refresh each API function's code, publish a new version, and point the
#      prod + test aliases at it.
#   5. Read stack outputs and generate frontend/config.js.
#   6. Upload the frontend to the private S3 bucket.
#   7. Invalidate the CloudFront cache.
#
# Usage: ./deploy.sh
#
# Environment overrides:
#   AWS_PROFILE  (default tr-yamashitam)   AWS_REGION   (default us-east-1)
#   PROJECT_NAME (default meallog)         STACK_NAME   (default meallog-demo)
#
set -euo pipefail

AWS_PROFILE="${AWS_PROFILE:-tr-yamashitam}"
AWS_REGION="${AWS_REGION:-us-east-1}"
PROJECT_NAME="${PROJECT_NAME:-meallog}"
STACK_NAME="${STACK_NAME:-meallog-demo}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEMPLATE="${DEMO_DIR}/cloudformation/meallog.yaml"
LAMBDA_DIR="${DEMO_DIR}/lambda"
FRONTEND_DIR="${DEMO_DIR}/frontend"
BUILD_DIR="${DEMO_DIR}/.build"

AWS=(aws --profile "${AWS_PROFILE}" --region "${AWS_REGION}")

# API 関数（common.py を同梱し、version + prod/test エイリアスを持つ）
API_FUNCS=(put_meal delete_meal list_recent query_rating query_favorite get_upload_url get_image_url)
# SQS ESM で動く非同期処理の関数（common.py 同梱、エイリアスなし）
WORKER_FUNCS=(process_image describe_image)

echo "=============================================="
echo " MealLog demo deploy"
echo "   profile : ${AWS_PROFILE}"
echo "   region  : ${AWS_REGION}"
echo "   stack   : ${STACK_NAME}"
echo "=============================================="

ACCOUNT_ID="$("${AWS[@]}" sts get-caller-identity --query Account --output text)"
CODE_BUCKET="${PROJECT_NAME}-lambda-code-${ACCOUNT_ID}"

# --- 1. Lambda code bucket ----------------------------------------------------
echo ">> Ensuring Lambda code bucket: ${CODE_BUCKET}"
if ! "${AWS[@]}" s3api head-bucket --bucket "${CODE_BUCKET}" 2>/dev/null; then
  if [ "${AWS_REGION}" = "us-east-1" ]; then
    "${AWS[@]}" s3api create-bucket --bucket "${CODE_BUCKET}" >/dev/null
  else
    "${AWS[@]}" s3api create-bucket --bucket "${CODE_BUCKET}" \
      --create-bucket-configuration LocationConstraint="${AWS_REGION}" >/dev/null
  fi
  echo "   created."
else
  echo "   already exists."
fi

# --- 2. Package and upload Lambdas -------------------------------------------
echo ">> Packaging Lambda functions"
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}"

# API functions: bundle handler.py + the shared common.py
for fn in "${API_FUNCS[@]}"; do
  stage="${BUILD_DIR}/${fn}"
  mkdir -p "${stage}"
  cp "${LAMBDA_DIR}/${fn}/handler.py" "${stage}/"
  cp "${LAMBDA_DIR}/common/common.py" "${stage}/"
  ( cd "${stage}" && zip -q -r "${BUILD_DIR}/${fn}.zip" . )
  "${AWS[@]}" s3 cp "${BUILD_DIR}/${fn}.zip" "s3://${CODE_BUCKET}/lambda/${fn}.zip" >/dev/null
  echo "   uploaded ${fn}.zip"
done

# 非同期ワーカー関数（common.py を同梱、エイリアスなし）
for fn in "${WORKER_FUNCS[@]}"; do
  stage="${BUILD_DIR}/${fn}"
  mkdir -p "${stage}"
  cp "${LAMBDA_DIR}/${fn}/handler.py" "${stage}/"
  cp "${LAMBDA_DIR}/common/common.py" "${stage}/"
  ( cd "${stage}" && zip -q -r "${BUILD_DIR}/${fn}.zip" . )
  "${AWS[@]}" s3 cp "${BUILD_DIR}/${fn}.zip" "s3://${CODE_BUCKET}/lambda/${fn}.zip" >/dev/null
  echo "   uploaded ${fn}.zip"
done

# Cognito trigger function (standalone)
( cd "${LAMBDA_DIR}/cognito_triggers" && zip -q -r "${BUILD_DIR}/cognito_triggers.zip" . )
"${AWS[@]}" s3 cp "${BUILD_DIR}/cognito_triggers.zip" "s3://${CODE_BUCKET}/lambda/cognito_triggers.zip" >/dev/null
echo "   uploaded cognito_triggers.zip"

# --- 2b. Build and publish the Pillow Lambda layer (manylinux) ----------------
# 画像処理 Lambda 用に Pillow を同梱した自前レイヤーを発行する。
# Lambda 実行環境(amd64/manylinux)向けに pip でビルドする。
echo ">> Building Pillow Lambda layer"
PILLOW_DIR="${BUILD_DIR}/pillow-layer/python"
mkdir -p "${PILLOW_DIR}"
python3 -m pip install \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  --target "${PILLOW_DIR}" \
  "pillow>=10,<12" >/dev/null
( cd "${BUILD_DIR}/pillow-layer" && zip -q -r "${BUILD_DIR}/pillow-layer.zip" . )
PILLOW_LAYER_ARN="$("${AWS[@]}" lambda publish-layer-version \
  --layer-name "${PROJECT_NAME}-pillow" \
  --description "Pillow for image processing (manylinux amd64, py3.12)" \
  --compatible-runtimes python3.12 \
  --compatible-architectures x86_64 \
  --zip-file "fileb://${BUILD_DIR}/pillow-layer.zip" \
  --query LayerVersionArn --output text)"
echo "   Pillow layer: ${PILLOW_LAYER_ARN}"

# --- 3. Deploy the stack ------------------------------------------------------
echo ">> Deploying CloudFormation stack (this can take several minutes)"
# テンプレートが大きい（>51200 bytes）ので S3 経由でアップロードする。
"${AWS[@]}" cloudformation deploy \
  --stack-name "${STACK_NAME}" \
  --template-file "${TEMPLATE}" \
  --s3-bucket "${CODE_BUCKET}" \
  --s3-prefix template \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    ProjectName="${PROJECT_NAME}" \
    LambdaCodeBucket="${CODE_BUCKET}" \
    PillowLayerArn="${PILLOW_LAYER_ARN}"

# --- 4. Refresh code, publish versions, repoint aliases ----------------------
# CloudFormation's Version resource is immutable, so after a code change we
# publish a fresh version and move the prod/test aliases to it ourselves.
echo ">> Refreshing API function code + versions + aliases"
for fn in "${API_FUNCS[@]}"; do
  # Function name uses hyphens (see template), dir uses underscores.
  fname="${PROJECT_NAME}-${fn//_/-}"
  "${AWS[@]}" lambda update-function-code --function-name "${fname}" \
    --s3-bucket "${CODE_BUCKET}" --s3-key "lambda/${fn}.zip" >/dev/null
  "${AWS[@]}" lambda wait function-updated --function-name "${fname}"
  VER="$("${AWS[@]}" lambda publish-version --function-name "${fname}" \
    --query Version --output text)"
  "${AWS[@]}" lambda update-alias --function-name "${fname}" --name prod \
    --function-version "${VER}" >/dev/null
  "${AWS[@]}" lambda update-alias --function-name "${fname}" --name test \
    --function-version "${VER}" >/dev/null
  echo "   ${fname}: version ${VER} -> prod, test"
done

# Cognito trigger (no alias)
"${AWS[@]}" lambda update-function-code --function-name "${PROJECT_NAME}-cognito-triggers" \
  --s3-bucket "${CODE_BUCKET}" --s3-key "lambda/cognito_triggers.zip" >/dev/null

# 非同期ワーカー（エイリアスなし、$LATEST を更新）
for fn in "${WORKER_FUNCS[@]}"; do
  fname="${PROJECT_NAME}-${fn//_/-}"
  "${AWS[@]}" lambda update-function-code --function-name "${fname}" \
    --s3-bucket "${CODE_BUCKET}" --s3-key "lambda/${fn}.zip" >/dev/null
  echo "   ${fname}: code refreshed (\$LATEST)"
done

# --- 5. Read outputs and build config.js -------------------------------------
echo ">> Reading stack outputs"
get_output() {
  "${AWS[@]}" cloudformation describe-stacks --stack-name "${STACK_NAME}" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

USER_POOL_ID="$(get_output UserPoolId)"
USER_POOL_CLIENT_ID="$(get_output UserPoolClientId)"
API_BASE_URL="$(get_output ApiBaseUrl)"
FRONTEND_BUCKET="$(get_output FrontendBucketName)"
DISTRIBUTION_ID="$(get_output CloudFrontDistributionId)"
APP_URL="$(get_output AppUrl)"

# API Gateway のメソッド変更を確実に反映するため、両ステージを再デプロイする
# （CloudFormation の Deployment リソースは静的なため、更新時の取りこぼし防止）。
REST_API_ID="$("${AWS[@]}" apigateway get-rest-apis \
  --query "items[?name=='${PROJECT_NAME}-api'].id" --output text)"
if [ -n "${REST_API_ID}" ]; then
  for st in prod test; do
    "${AWS[@]}" apigateway create-deployment --rest-api-id "${REST_API_ID}" \
      --stage-name "${st}" >/dev/null 2>&1 || true
  done
  echo ">> Redeployed API stages (prod/test)"
fi

# If the seeded sample user exists, surface its sub/name to the frontend so it
# appears in the "target user" dropdown. Safe to skip if seed_data.sh hasn't run.
SAMPLE_USERNAME="${SEED_USERNAME:-demofan100}"
SAMPLE_SUB="$("${AWS[@]}" cognito-idp admin-get-user --user-pool-id "${USER_POOL_ID}" \
  --username "${SAMPLE_USERNAME}" --query "UserAttributes[?Name=='sub'].Value" \
  --output text 2>/dev/null || true)"
if [ -z "${SAMPLE_SUB}" ] || [ "${SAMPLE_SUB}" = "None" ]; then
  SAMPLE_SUB=""
  SAMPLE_USERNAME=""
  echo "   (sample user not found yet; run seed_data.sh then deploy again to list it)"
else
  echo "   sample user ${SAMPLE_USERNAME} -> ${SAMPLE_SUB}"
fi

cat > "${FRONTEND_DIR}/config.js" <<EOF
// Generated by deploy.sh on $(date -u +"%Y-%m-%dT%H:%M:%SZ")
window.MEALLOG_CONFIG = {
  region: "${AWS_REGION}",
  userPoolId: "${USER_POOL_ID}",
  userPoolClientId: "${USER_POOL_CLIENT_ID}",
  apiBaseUrl: "${API_BASE_URL}",
  sampleUserSub: "${SAMPLE_SUB}",
  sampleUserName: "${SAMPLE_USERNAME}"
};
EOF
echo "   wrote frontend/config.js"

# --- 6. Upload the frontend ---------------------------------------------------
echo ">> Uploading frontend to s3://${FRONTEND_BUCKET}"
"${AWS[@]}" s3 sync "${FRONTEND_DIR}" "s3://${FRONTEND_BUCKET}" --delete --exclude ".DS_Store"

# --- 7. Invalidate CloudFront -------------------------------------------------
echo ">> Invalidating CloudFront cache"
"${AWS[@]}" cloudfront create-invalidation \
  --distribution-id "${DISTRIBUTION_ID}" --paths "/*" >/dev/null || true

echo ""
echo "=============================================="
echo " Deploy complete!"
echo "   App URL : ${APP_URL}"
echo "   (CloudFront can take a few minutes to finish deploying.)"
echo "=============================================="
