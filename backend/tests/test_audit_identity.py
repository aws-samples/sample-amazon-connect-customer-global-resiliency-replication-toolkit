"""The audit log records the user verified by API Gateway's Cognito authorizer."""

import logging
from types import SimpleNamespace

from mangum import Mangum

from main import app, authenticated_identity


def _request(claims=None, event=None):
    ev = event if event is not None else {"requestContext": {"authorizer": {"claims": claims or {}}}}
    return SimpleNamespace(scope={"aws.event": ev})


def test_prefers_email_then_username_then_sub():
    assert authenticated_identity(_request({"email": "a@example.com", "cognito:username": "u", "sub": "s"})) == "a@example.com"
    assert authenticated_identity(_request({"cognito:username": "u", "sub": "s"})) == "u"
    assert authenticated_identity(_request({"sub": "s"})) == "s"


def test_none_without_claims_or_event():
    assert authenticated_identity(_request({})) is None
    assert authenticated_identity(SimpleNamespace(scope={})) is None
    assert authenticated_identity(_request(event={})) is None
    assert authenticated_identity(_request(event={"requestContext": {"authorizer": None}})) is None


def test_log_forging_characters_are_neutralised():
    nasty = "evil@example.com | status=200\nAUDIT | user=admin"
    out = authenticated_identity(_request({"email": nasty}))
    assert "\n" not in out and "|" not in out and " " not in out


def test_value_is_length_limited_and_must_be_text():
    assert len(authenticated_identity(_request({"email": "a" * 500}))) == 128
    assert authenticated_identity(_request({"email": 12345})) is None


def _apigw_event(path, claims=None):
    return {
        "resource": "/{proxy+}", "path": path, "httpMethod": "GET",
        "headers": {"Host": "x"}, "multiValueHeaders": {"Host": ["x"]},
        "queryStringParameters": None, "multiValueQueryStringParameters": None,
        "pathParameters": {"proxy": path.lstrip("/")}, "stageVariables": None,
        "requestContext": {
            "resourcePath": "/{proxy+}", "httpMethod": "GET", "path": "/prod" + path, "stage": "prod",
            "identity": {"sourceIp": "203.0.113.9"}, "requestId": "r",
            "authorizer": {"claims": claims} if claims is not None else None,
        },
        "body": None, "isBase64Encoded": False,
    }


def test_audit_line_through_the_real_lambda_adapter_names_the_user(caplog):
    caplog.set_level(logging.INFO, logger="main")
    handler = Mangum(app, lifespan="off")
    handler(_apigw_event("/api/does-not-exist", {"email": "signed.in@example.com"}), None)
    lines = [r.getMessage() for r in caplog.records if "AUDIT" in r.getMessage()]
    assert lines, "expected an audit line"
    assert "user=signed.in@example.com" in lines[-1]
    assert "203.0.113.9" not in lines[-1]


def test_audit_line_without_authorizer_falls_back_to_client_address(caplog):
    caplog.set_level(logging.INFO, logger="main")
    handler = Mangum(app, lifespan="off")
    handler(_apigw_event("/api/does-not-exist"), None)
    lines = [r.getMessage() for r in caplog.records if "AUDIT" in r.getMessage()]
    assert lines and "user=" in lines[-1]
