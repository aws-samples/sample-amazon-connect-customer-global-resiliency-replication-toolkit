#!/bin/bash
# =============================================================================
# Deploy script for Connect ACGR Resource Replicator
# =============================================================================
# This script:
#   1. Builds the frontend (React/Vite)
#   2. Bundles backend Python dependencies into the Lambda package
#   3. Runs CDK synth to validate the CloudFormation template
#   4. Deploys the CDK stack to your AWS account
#
# Prerequisites:
#   - AWS CLI configured with credentials (aws sts get-caller-identity)
#   - Node.js 18+ and npm
#   - Python 3.12+
#   - AWS CDK CLI (npm install -g aws-cdk)
#
# Usage:
#   cd acgr-replication-starter-pack
#   chmod +x deploy.sh
#   ./deploy.sh              # Full deploy
#   ./deploy.sh synth        # Just synthesize (no deploy)
#   ./deploy.sh destroy      # Tear down the stack
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
FRONTEND_DIR="$SCRIPT_DIR/frontend"
INFRA_DIR="$SCRIPT_DIR/infra"

ACTION="${1:-deploy}"

echo "============================================"
echo "Connect ACGR Resource Replicator - Deployment"
echo "============================================"
echo ""

# -------------------------------------------------------------------
# Step 0: Preflight checks
# -------------------------------------------------------------------
echo ">>> Step 0: Preflight checks..."

if ! command -v aws &> /dev/null; then
    echo "ERROR: AWS CLI not found. Install it: https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html"
    exit 1
fi

if ! command -v cdk &> /dev/null; then
    echo "ERROR: AWS CDK CLI not found. Install it: npm install -g aws-cdk"
    exit 1
fi

if ! command -v node &> /dev/null; then
    echo "ERROR: Node.js not found."
    exit 1
fi

if ! command -v python3 &> /dev/null; then
    echo "ERROR: Python 3 not found."
    exit 1
fi

echo "  AWS CLI:  $(aws --version 2>&1 | head -1)"
echo "  CDK CLI:  $(cdk --version)"
echo "  Node.js:  $(node --version)"
echo "  Python:   $(python3 --version)"
echo ""

# Verify AWS credentials
echo ">>> Verifying AWS credentials..."
aws sts get-caller-identity --output table || {
    echo "ERROR: AWS credentials not configured. Run 'aws configure' or set AWS_PROFILE."
    exit 1
}
echo ""

# -------------------------------------------------------------------
# Step 1: Build frontend
# -------------------------------------------------------------------
echo ">>> Step 1: Building frontend..."
cd "$FRONTEND_DIR"
npm ci --silent
npm run build
echo "  Frontend built -> $FRONTEND_DIR/dist/"
echo ""

# -------------------------------------------------------------------
# Step 2: Bundle backend dependencies into Lambda package
# -------------------------------------------------------------------
echo ">>> Step 2: Bundling backend Python dependencies..."

# Install dependencies into the backend directory so CDK's from_asset picks them up.
# We use a temporary directory to avoid polluting the source tree, then copy.
DEPS_DIR="$BACKEND_DIR/.lambda_deps"
rm -rf "$DEPS_DIR"

# Install pure-Python packages normally (exclude pydantic/pydantic-core — we pin them below)
pip3 install \
    --target "$DEPS_DIR" \
    --quiet \
    --no-user \
    fastapi mangum boto3 uvicorn requests

# Install pydantic + pydantic-core with Linux x86_64 binaries for Lambda.
# We install them together so pip resolves compatible versions automatically.
pip3 install \
    --target "$DEPS_DIR" \
    --quiet \
    --no-user \
    --platform manylinux2014_x86_64 \
    --implementation cp \
    --python-version 3.12 \
    --only-binary=:all: \
    --upgrade \
    pydantic pydantic-core

# Copy deps into backend root (CDK bundles the whole backend/ folder)
cp -r "$DEPS_DIR"/* "$BACKEND_DIR/" 2>/dev/null || true
rm -rf "$DEPS_DIR"

echo "  Backend dependencies bundled into $BACKEND_DIR/"
echo ""

# -------------------------------------------------------------------
# Step 3: Install CDK dependencies
# -------------------------------------------------------------------
echo ">>> Step 3: Installing CDK Python dependencies..."
cd "$INFRA_DIR"
pip3 install -r requirements.txt --quiet --no-user
echo ""

# -------------------------------------------------------------------
# Step 4: CDK synth / deploy / destroy
# -------------------------------------------------------------------
cd "$INFRA_DIR"

if [ "$ACTION" = "synth" ]; then
    echo ">>> Step 4: Synthesizing CloudFormation template..."
    cdk synth
    echo ""
    echo "Template synthesized. Check $INFRA_DIR/cdk.out/"

elif [ "$ACTION" = "destroy" ]; then
    echo ">>> Step 4: Destroying stack..."
    cdk destroy --force
    echo ""
    echo "Stack destroyed."

    # Clean up bundled deps
    echo ">>> Cleaning up bundled dependencies..."
    cd "$BACKEND_DIR"
    # Remove known dependency directories (not our source code)
    for pkg in fastapi mangum boto3 botocore pydantic uvicorn starlette anyio sniffio \
               typing_extensions annotated_types pydantic_core s3transfer jmespath \
               urllib3 idna certifi charset_normalizer click h11 httptools uvloop \
               watchfiles websockets python_multipart email_validator dnspython; do
        rm -rf "$BACKEND_DIR/$pkg" "$BACKEND_DIR/${pkg}.dist-info" "$BACKEND_DIR/${pkg}-"* 2>/dev/null || true
    done
    rm -rf "$BACKEND_DIR/bin" 2>/dev/null || true
    echo "  Cleaned up."

else
    echo ">>> Step 4: Deploying CDK stack..."
    echo ""
    echo "  This will create the following AWS resources:"
    echo "    - Lambda function (FastAPI backend)"
    echo "    - API Gateway REST API"
    echo "    - S3 bucket (frontend assets)"
    echo "    - CloudFront distribution"
    echo "    - DynamoDB table (sessions)"
    echo "    - IAM roles"
    echo ""

    # Bootstrap CDK if needed (first-time deployment in this account/region)
    cdk bootstrap 2>/dev/null || true

    cdk deploy --require-approval never --outputs-file "$SCRIPT_DIR/cdk-outputs.json"

    echo ""
    echo "============================================"
    echo "Deployment complete!"
    echo "============================================"

    if [ -f "$SCRIPT_DIR/cdk-outputs.json" ]; then
        echo ""
        echo "Stack outputs:"
        cat "$SCRIPT_DIR/cdk-outputs.json"
        echo ""
    fi

    # -------------------------------------------------------------------
    # Step 5: Upload frontend to S3
    # -------------------------------------------------------------------
    echo ">>> Step 5: Uploading frontend to S3..."

    # Extract the S3 bucket name from CDK outputs or CloudFormation
    BUCKET_NAME=$(aws cloudformation describe-stack-resources \
        --stack-name ConnectAcgrReplicatorStack \
        --query "StackResources[?ResourceType=='AWS::S3::Bucket' && starts_with(LogicalResourceId, 'FrontendBucket')].PhysicalResourceId" \
        --output text)

    if [ -n "$BUCKET_NAME" ]; then
        # Upload hashed assets with long cache (1 year) — filenames contain content hash
        aws s3 sync "$FRONTEND_DIR/dist/assets/" "s3://$BUCKET_NAME/assets/" \
            --delete \
            --cache-control "public, max-age=31536000, immutable"
        # Upload index.html with no-cache so browsers always fetch the latest
        aws s3 cp "$FRONTEND_DIR/dist/index.html" "s3://$BUCKET_NAME/index.html" \
            --cache-control "no-cache, no-store, must-revalidate"
        # Upload any other root files (favicon, etc.) with short cache
        aws s3 sync "$FRONTEND_DIR/dist/" "s3://$BUCKET_NAME/" \
            --delete \
            --exclude "assets/*" \
            --exclude "index.html" \
            --cache-control "public, max-age=300"
        echo "  Frontend uploaded to s3://$BUCKET_NAME/ (with cache-control headers)"
    else
        echo "  WARNING: Could not find frontend S3 bucket. Upload manually:"
        echo "  aws s3 sync $FRONTEND_DIR/dist/ s3://<BUCKET_NAME>/ --delete"
    fi

    # Extract CloudFront distribution ID for invalidation
    CF_DIST_ID=$(aws cloudformation describe-stack-resources \
        --stack-name ConnectAcgrReplicatorStack \
        --query "StackResources[?ResourceType=='AWS::CloudFront::Distribution'].PhysicalResourceId" \
        --output text)

    if [ -n "$CF_DIST_ID" ]; then
        echo ">>> Invalidating CloudFront cache..."
        aws cloudfront create-invalidation --distribution-id "$CF_DIST_ID" --paths "/*" > /dev/null
        echo "  CloudFront cache invalidated."
    fi

    echo ""
    echo "============================================"
    echo "Your app is live!"
    echo "============================================"
fi
