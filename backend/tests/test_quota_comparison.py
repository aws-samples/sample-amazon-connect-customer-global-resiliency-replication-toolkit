"""Quota comparison: Connect quotas for ACGR-mirrored resources are flagged."""

from unittest.mock import patch

from quota.quota_comparison import _ACGR_AUTO_MATCHED_CODES, compare_quotas


def _run(source_val, target_val):
    def _value(_client, _service, _code):
        return {"value": source_val if _client == "src" else target_val,
                "is_applied": False, "adjustable": True}

    with patch("quota.quota_comparison._check_acgr_enabled",
               return_value={"target_region": "us-west-2"}), \
         patch("quota.quota_comparison.create_client",
               side_effect=lambda svc, region: "src" if region == "us-east-1" else "tgt"), \
         patch("quota.quota_comparison._get_quota_value", side_effect=_value):
        return compare_quotas("arn:aws:connect:us-east-1:123456789012:instance/abc", "us-east-1")


def _row(result, code):
    return next(r for r in result["comparison"] if r["quota_code"] == code)


def test_mirrored_resource_quota_is_flagged_and_explained():
    result = _run(500, 100)
    users = _row(result, "L-9A46857E")  # Users per instance
    assert users["acgr_auto_matched"] is True
    assert "matches this quota automatically" in users["discrepancy"]


def test_quotas_acgr_does_not_match_are_not_flagged():
    result = _run(500, 100)
    for code in ("L-8F812903",   # Phone numbers per instance
                 "L-12AB7C57",   # Concurrent active calls per instance
                 "L-E3D2F503"):  # Lambda functions per instance
        row = _row(result, code)
        assert row["acgr_auto_matched"] is False
        assert "matches this quota automatically" not in row["discrepancy"]
    assert all(not r["acgr_auto_matched"] for r in result["comparison"] if r["service"] != "Amazon Connect")


def test_flagged_codes_are_all_in_the_connect_list():
    from quota.quota_comparison import _SERVICE_QUOTAS
    connect_codes = {c for c, _ in next(s for s in _SERVICE_QUOTAS if s["service_code"] == "connect")["quotas"]}
    assert _ACGR_AUTO_MATCHED_CODES <= connect_codes
