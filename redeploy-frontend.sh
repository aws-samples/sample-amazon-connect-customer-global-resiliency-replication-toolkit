#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FRONTEND_DIR="$SCRIPT_DIR/frontend"

echo ">>> Building frontend..."
cd "$FRONTEND_DIR"
npm run build 2>&1
echo "  Frontend built."

echo ""
echo ">>> Uploading to S3..."
BUCKET_NAME=$(aws cloudformation describe-stack-resources \
    --stack-name ConnectAcgrReplicatorStack \
    --query "StackResources[?ResourceType=='AWS::S3::Bucket' && starts_with(LogicalResourceId, 'FrontendBucket')].PhysicalResourceId" \
    --output text 2>&1)

if [ -z "$BUCKET_NAME" ]; then
    echo "ERROR: Could not find S3 bucket."
    exit 1
fi

echo "  Bucket: $BUCKET_NAME"
aws s3 sync "$FRONTEND_DIR/dist/" "s3://$BUCKET_NAME/" --delete 2>&1

echo ""
echo ">>> Invalidating CloudFront cache..."
CF_DIST_ID=$(aws cloudformation describe-stack-resources \
    --stack-name ConnectAcgrReplicatorStack \
    --query "StackResources[?ResourceType=='AWS::CloudFront::Distribution'].PhysicalResourceId" \
    --output text 2>&1)

if [ -n "$CF_DIST_ID" ]; then
    aws cloudfront create-invalidation --distribution-id "$CF_DIST_ID" --paths "/*" 2>&1
    echo "  CloudFront invalidated."
else
    echo "  WARNING: Could not find CloudFront distribution."
fi

echo ""
echo "Done! Give CloudFront ~30-60 seconds to propagate, then refresh your browser."
