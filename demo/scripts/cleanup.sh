#!/usr/bin/env bash
#
# cleanup.sh - Tear down the entire MealLog demo environment.
#
# Steps:
#   1. Empty the frontend S3 bucket (CloudFormation cannot delete a non-empty bucket).
#   2. Delete the CloudFormation stack (removes DynamoDB, Lambda, API Gateway,
#      Cognito, CloudFront, the frontend bucket, IAM roles, etc.).
#   3. Empty and delete the Lambda code bucket (created outside the stack).
#
# Usage:
#   ./cleanup.sh
#
# Environment overrides (must match deploy.sh):
#   AWS_PROFILE   (未指定なら既定プロファイル)
#   AWS_REGION    (default: us-east-1)
#   PROJECT_NAME  (default: meallog)
#   STACK_NAME    (default: meallog-demo)
#
set -euo pipefail

AWS_PROFILE="${AWS_PROFILE:-}"
AWS_REGION="${AWS_REGION:-us-east-1}"
PROJECT_NAME="${PROJECT_NAME:-meallog}"
STACK_NAME="${STACK_NAME:-meallog-demo}"

AWS=(aws --region "${AWS_REGION}")
[ -n "${AWS_PROFILE}" ] && AWS+=(--profile "${AWS_PROFILE}")

echo "=============================================="
echo " MealLog demo cleanup"
echo "   profile : ${AWS_PROFILE:-(default)}"
echo "   region  : ${AWS_REGION}"
echo "   stack   : ${STACK_NAME}"
echo "=============================================="

ACCOUNT_ID="$("${AWS[@]}" sts get-caller-identity --query Account --output text)"
CODE_BUCKET="${PROJECT_NAME}-lambda-code-${ACCOUNT_ID}"
FRONTEND_BUCKET="${PROJECT_NAME}-frontend-${ACCOUNT_ID}"
UPLOAD_BUCKET="${PROJECT_NAME}-uploads-${ACCOUNT_ID}"
OUTPUT_BUCKET="${PROJECT_NAME}-images-${ACCOUNT_ID}"

empty_bucket() {
  local bucket="$1"
  if "${AWS[@]}" s3api head-bucket --bucket "${bucket}" 2>/dev/null; then
    echo ">> Emptying s3://${bucket}"
    "${AWS[@]}" s3 rm "s3://${bucket}" --recursive >/dev/null || true
  fi
}

# --- 1. Empty the stack-managed buckets (must be empty before deletion)
empty_bucket "${FRONTEND_BUCKET}"
empty_bucket "${UPLOAD_BUCKET}"
empty_bucket "${OUTPUT_BUCKET}"

# --- 2. Delete the stack ------------------------------------------------------
if "${AWS[@]}" cloudformation describe-stacks --stack-name "${STACK_NAME}" >/dev/null 2>&1; then
  echo ">> Deleting CloudFormation stack: ${STACK_NAME}"
  "${AWS[@]}" cloudformation delete-stack --stack-name "${STACK_NAME}"
  echo "   waiting for stack deletion to complete..."
  "${AWS[@]}" cloudformation wait stack-delete-complete --stack-name "${STACK_NAME}"
  echo "   stack deleted."
else
  echo ">> Stack ${STACK_NAME} not found (already deleted?)"
fi

# --- 3. Remove the Lambda code bucket ----------------------------------------
if "${AWS[@]}" s3api head-bucket --bucket "${CODE_BUCKET}" 2>/dev/null; then
  empty_bucket "${CODE_BUCKET}"
  echo ">> Deleting bucket s3://${CODE_BUCKET}"
  "${AWS[@]}" s3api delete-bucket --bucket "${CODE_BUCKET}" >/dev/null
  echo "   deleted."
fi

# --- 4. Delete all versions of the Pillow layer (published outside the stack) -
echo ">> Deleting Pillow layer versions"
for v in $("${AWS[@]}" lambda list-layer-versions --layer-name "${PROJECT_NAME}-pillow" \
             --query "LayerVersions[].Version" --output text 2>/dev/null); do
  "${AWS[@]}" lambda delete-layer-version --layer-name "${PROJECT_NAME}-pillow" --version-number "${v}" 2>/dev/null || true
done

echo ""
echo "=============================================="
echo " Cleanup complete. All demo resources removed."
echo "=============================================="
