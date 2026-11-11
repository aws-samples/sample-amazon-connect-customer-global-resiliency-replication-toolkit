#!/usr/bin/env bash
# Create a sign-in user for the Replicator UI.
#
#   scripts/create-user.sh user@example.com [STACK_NAME]
#
# Amazon Cognito emails a temporary password; the user is asked to choose a new
# one at first sign-in. Self sign-up is disabled, so this is the only way in.
set -euo pipefail

EMAIL="${1:?Usage: $0 user@example.com [STACK_NAME]}"
STACK="${2:-ConnectAcgrReplicatorStack}"

case "$EMAIL" in
    *@*.*) ;;
    *) echo "ERROR: '$EMAIL' does not look like an email address." >&2; exit 1 ;;
esac

POOL="$(aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text)"
if [ -z "$POOL" ] || [ "$POOL" = "None" ]; then
    echo "ERROR: could not read UserPoolId from stack $STACK." >&2
    exit 1
fi

aws cognito-idp admin-create-user \
    --user-pool-id "$POOL" \
    --username "$EMAIL" \
    --user-attributes "Name=email,Value=$EMAIL" "Name=email_verified,Value=true" \
    --desired-delivery-mediums EMAIL \
    --query 'User.[Username,UserStatus]' --output text
echo "Created $EMAIL. A temporary password was emailed to them."
