"""Property and example tests for the synthesized CloudFormation template.

Each test exercises an invariant described in the design document:

- ``test_no_wildcard_cors``         — Property 1 (Requirements 1.4)
- ``test_dynamodb_retain_policy``   — Property 2 (Requirements 1.6)
- ``test_lambda_log_level_info``    — Property 3 (Requirements 1.7)
- ``test_usage_plan_present``       — Example test (Requirements 1.5)
"""

from typing import Any, Iterator


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_ORIGIN_KEYS = {
    "AllowOrigin",
    "AccessControlAllowOrigin",
    "Access-Control-Allow-Origin",
    "method.response.header.Access-Control-Allow-Origin",
}

# Logical-ID substrings that identify CDK-auto-generated helper Lambdas we do
# not author and therefore do not expect LOG_LEVEL=INFO on.
_AUTO_HELPER_MARKERS = ("AutoDeleteObjects", "LogRetention", "CustomResource")


def _walk(node: Any) -> Iterator[tuple[str, Any]]:
    """Yield ``(key, value)`` pairs for every dict entry reachable from node."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _is_wildcard(value: Any) -> bool:
    """True when the given value is an API-Gateway wildcard origin literal.

    CDK tokens (``{"Fn::Join": ...}`` / ``{"Fn::GetAtt": ...}`` dicts) are
    considered safe because they resolve to the CloudFront distribution
    domain at deploy time.
    """
    if not isinstance(value, str):
        return False
    return value in ("*", "'*'")


# --------------------------------------------------------------------------- #
# Property 1 — No wildcard CORS origin (Requirements 1.4)
# --------------------------------------------------------------------------- #

def test_no_wildcard_cors(template_json: dict) -> None:
    """Validates: Requirements 1.4.

    For every ``AWS::ApiGateway::*`` resource, recursively walk its properties
    and assert that no ``Allow-Origin``-style key carries a literal ``"*"``.
    """
    resources = template_json["Resources"]
    apigw_resources = {
        logical_id: resource
        for logical_id, resource in resources.items()
        if resource.get("Type", "").startswith("AWS::ApiGateway")
    }
    assert apigw_resources, "expected at least one AWS::ApiGateway resource"

    offenders: list[tuple[str, str, Any]] = []
    for logical_id, resource in apigw_resources.items():
        for key, value in _walk(resource):
            if key in _ORIGIN_KEYS and _is_wildcard(value):
                offenders.append((logical_id, key, value))

    assert not offenders, (
        "Wildcard CORS origin detected in synthesized template: "
        f"{offenders!r}"
    )


# --------------------------------------------------------------------------- #
# Property 2 — DynamoDB tables are retained (Requirements 1.6)
# --------------------------------------------------------------------------- #

def test_dynamodb_retain_policy(template_json: dict) -> None:
    """Validates: Requirements 1.6.

    Every ``AWS::DynamoDB::Table`` must carry ``DeletionPolicy: Retain`` and
    ``UpdateReplacePolicy: Retain`` so a ``cdk destroy`` orphans the data
    rather than deleting it.
    """
    tables = {
        logical_id: resource
        for logical_id, resource in template_json["Resources"].items()
        if resource.get("Type") == "AWS::DynamoDB::Table"
    }
    assert len(tables) >= 2, (
        f"expected at least 2 DynamoDB tables (ReplicatorSessions + "
        f"FlowAnalysisJobs), found {len(tables)}: {list(tables)}"
    )

    for logical_id, resource in tables.items():
        assert resource.get("DeletionPolicy") == "Retain", (
            f"{logical_id}: DeletionPolicy is "
            f"{resource.get('DeletionPolicy')!r}, expected 'Retain'"
        )
        assert resource.get("UpdateReplacePolicy") == "Retain", (
            f"{logical_id}: UpdateReplacePolicy is "
            f"{resource.get('UpdateReplacePolicy')!r}, expected 'Retain'"
        )


# --------------------------------------------------------------------------- #
# Property 3 — LOG_LEVEL=INFO on every author-written Lambda (Requirements 1.7)
# --------------------------------------------------------------------------- #

def test_lambda_log_level_info(template_json: dict) -> None:
    """Validates: Requirements 1.7.

    Every Lambda we author in the stack must have ``LOG_LEVEL=INFO`` set in
    its environment. CDK auto-generated helper Lambdas (custom-resource
    handlers, log-retention helpers) are skipped because we don't own their
    environment configuration.
    """
    lambdas = {
        logical_id: resource
        for logical_id, resource in template_json["Resources"].items()
        if resource.get("Type") == "AWS::Lambda::Function"
    }

    author_lambdas = {
        logical_id: resource
        for logical_id, resource in lambdas.items()
        if not any(marker in logical_id for marker in _AUTO_HELPER_MARKERS)
    }
    assert len(author_lambdas) >= 2, (
        f"expected at least 2 author-written Lambdas (ResourceReplicatorLambda"
        f" + ReplicatorBackend), found {len(author_lambdas)}: "
        f"{list(author_lambdas)}"
    )

    for logical_id, resource in author_lambdas.items():
        properties = resource.get("Properties", {})
        environment = properties.get("Environment", {})
        variables = environment.get("Variables", {})
        assert "LOG_LEVEL" in variables, (
            f"{logical_id}: Environment.Variables is missing LOG_LEVEL; "
            f"variables set: {list(variables)}"
        )
        assert variables["LOG_LEVEL"] == "INFO", (
            f"{logical_id}: LOG_LEVEL is {variables['LOG_LEVEL']!r}, "
            f"expected 'INFO'"
        )


# --------------------------------------------------------------------------- #
# Example test — UsagePlan is present with the expected throttle (Requirements 1.5)
# --------------------------------------------------------------------------- #

def test_usage_plan_present(template_json: dict) -> None:
    """Validates: Requirements 1.5.

    Exactly one ``AWS::ApiGateway::UsagePlan`` must exist and its throttle
    must be 1000 req/s rate limit / 2000 burst.
    """
    usage_plans = [
        (logical_id, resource)
        for logical_id, resource in template_json["Resources"].items()
        if resource.get("Type") == "AWS::ApiGateway::UsagePlan"
    ]
    assert len(usage_plans) == 1, (
        f"expected exactly 1 AWS::ApiGateway::UsagePlan, "
        f"found {len(usage_plans)}: {[lid for lid, _ in usage_plans]}"
    )

    _, plan = usage_plans[0]
    throttle = plan.get("Properties", {}).get("Throttle", {})
    assert throttle.get("RateLimit") == 1000, (
        f"UsagePlan RateLimit is {throttle.get('RateLimit')!r}, expected 1000"
    )
    assert throttle.get("BurstLimit") == 2000, (
        f"UsagePlan BurstLimit is {throttle.get('BurstLimit')!r}, expected 2000"
    )


# --------------------------------------------------------------------------- #
# Authentication — Amazon Cognito user pool + API Gateway authorizer
# --------------------------------------------------------------------------- #

def _resources(template_json: dict, type_name: str) -> list[dict]:
    return [r for r in template_json["Resources"].values() if r["Type"] == type_name]


def test_every_api_method_requires_cognito_except_cors_preflight(template_json: dict) -> None:
    """No API Gateway method may be reachable without a Cognito token.

    The only unauthenticated methods allowed are the CORS preflight ``OPTIONS``
    methods: browsers send no credentials on a preflight and it returns no data.
    """
    methods = _resources(template_json, "AWS::ApiGateway::Method")
    assert methods, "expected API Gateway methods in the template"
    for m in methods:
        props = m["Properties"]
        if props["HttpMethod"] == "OPTIONS":
            continue
        assert props.get("AuthorizationType") == "COGNITO_USER_POOLS", props
        assert "AuthorizerId" in props, props


def test_api_authorizer_is_cognito_and_reads_authorization_header(template_json: dict) -> None:
    authorizers = _resources(template_json, "AWS::ApiGateway::Authorizer")
    assert len(authorizers) == 1
    props = authorizers[0]["Properties"]
    assert props["Type"] == "COGNITO_USER_POOLS"
    assert props["IdentitySource"] == "method.request.header.Authorization"
    assert props["ProviderARNs"], "authorizer must name the user pool"


def test_user_pool_is_admin_managed_with_strong_passwords(template_json: dict) -> None:
    pools = _resources(template_json, "AWS::Cognito::UserPool")
    assert len(pools) == 1
    props = pools[0]["Properties"]
    # No self sign-up: only administrators create users.
    assert props["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True
    policy = props["Policies"]["PasswordPolicy"]
    assert policy["MinimumLength"] >= 12
    assert policy["RequireLowercase"] and policy["RequireUppercase"]
    assert policy["RequireNumbers"] and policy["RequireSymbols"]
    assert props["UsernameAttributes"] == ["email"]


def test_user_pool_is_retained_on_destroy(template_json: dict) -> None:
    pools = [r for r in template_json["Resources"].values() if r["Type"] == "AWS::Cognito::UserPool"]
    assert pools[0].get("DeletionPolicy") == "Retain"


def test_app_client_is_public_pkce_code_flow_only(template_json: dict) -> None:
    clients = _resources(template_json, "AWS::Cognito::UserPoolClient")
    assert len(clients) == 1
    props = clients[0]["Properties"]
    assert props["GenerateSecret"] is False            # public client (a browser app)
    assert props["AllowedOAuthFlows"] == ["code"]      # no implicit flow
    assert props["AllowedOAuthFlowsUserPoolClient"] is True
    assert props["PreventUserExistenceErrors"] == "ENABLED"
    # No direct password / SRP / custom API sign-in; only token refresh.
    assert props["ExplicitAuthFlows"] == ["ALLOW_REFRESH_TOKEN_AUTH"]
    # Callback and logout URLs: the CloudFront distribution only (no wildcard, no http).
    for url in props["CallbackURLs"] + props["LogoutURLs"]:
        flat = str(url)
        assert "*" not in flat
        assert "http://" not in flat


def test_stack_outputs_for_sign_in_configuration(template_json: dict) -> None:
    outputs = template_json["Outputs"]
    for name in ("UserPoolId", "UserPoolClientId", "CognitoDomain", "CloudFrontUrl"):
        assert name in outputs, f"missing stack output {name}"
