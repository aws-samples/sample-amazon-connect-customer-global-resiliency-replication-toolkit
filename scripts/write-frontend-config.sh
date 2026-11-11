#!/usr/bin/env bash
# Write the UI's sign-in configuration (config.json) from the CloudFormation
# stack outputs.
#
#   scripts/write-frontend-config.sh [STACK_NAME] [OUTPUT_DIR]
#
# Defaults: STACK_NAME=ConnectAcgrReplicatorStack, OUTPUT_DIR=frontend/dist
# The Region comes from AWS_REGION / AWS_DEFAULT_REGION / your AWS CLI config.
#
# config.json holds identifiers only (user pool ID, app client ID, hosted
# sign-in domain). The app client is public: no secret is written.
set -euo pipefail

STACK="${1:-ConnectAcgrReplicatorStack}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${2:-$SCRIPT_DIR/../frontend/dist}"

output() {
    aws cloudformation describe-stacks --stack-name "$STACK" \
        --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

USER_POOL_ID="$(output UserPoolId)"
CLIENT_ID="$(output UserPoolClientId)"
DOMAIN="$(output CognitoDomain)"

for v in USER_POOL_ID CLIENT_ID DOMAIN; do
    val="${!v}"
    if [ -z "$val" ] || [ "$val" = "None" ]; then
        echo "ERROR: stack $STACK has no output for $v. Deploy the stack first." >&2
        exit 1
    fi
done

mkdir -p "$OUT_DIR"
python3 - "$OUT_DIR/config.json" "$USER_POOL_ID" "$CLIENT_ID" "$DOMAIN" <<'PY'
import json, sys
path, pool, client, domain = sys.argv[1:5]
config = {
    "region": pool.split("_", 1)[0],
    "userPoolId": pool,
    "clientId": client,
    "domain": domain,
}
with open(path, "w") as f:
    json.dump(config, f, indent=2)
    f.write("\n")
PY
echo "  Wrote $OUT_DIR/config.json (user pool $USER_POOL_ID)"
