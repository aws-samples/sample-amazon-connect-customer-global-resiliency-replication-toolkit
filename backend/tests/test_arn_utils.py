"""Unit tests for ARN utilities and ACGR region pair mapping."""

import pytest

from aws.arn_utils import (
    ACGR_REGION_PAIRS,
    extract_region,
    parse_arn,
    resolve_target_region,
    rewrite_arn,
)


# ---------------------------------------------------------------------------
# parse_arn
# ---------------------------------------------------------------------------

class TestParseArn:
    def test_standard_lambda_arn(self):
        arn = "arn:aws:lambda:us-west-2:123456789012:function:my-func"
        result = parse_arn(arn)
        assert result == {
            "partition": "aws",
            "service": "lambda",
            "region": "us-west-2",
            "account": "123456789012",
            "resource": "function:my-func",
        }

    def test_dynamodb_table_arn(self):
        arn = "arn:aws:dynamodb:eu-west-2:111111111111:table/MyTable"
        result = parse_arn(arn)
        assert result["service"] == "dynamodb"
        assert result["region"] == "eu-west-2"
        assert result["resource"] == "table/MyTable"

    def test_iam_role_arn_no_region(self):
        """IAM ARNs have an empty region component."""
        arn = "arn:aws:iam::123456789012:role/my-role"
        result = parse_arn(arn)
        assert result["region"] == ""
        assert result["service"] == "iam"

    def test_govcloud_partition(self):
        arn = "arn:aws-us-gov:lambda:us-gov-west-1:123456789012:function:f"
        result = parse_arn(arn)
        assert result["partition"] == "aws-us-gov"
        assert result["region"] == "us-gov-west-1"

    def test_resource_with_colons(self):
        """Resource part may contain colons (e.g., function:name:qualifier)."""
        arn = "arn:aws:lambda:us-east-1:123456789012:function:my-func:PROD"
        result = parse_arn(arn)
        assert result["resource"] == "function:my-func:PROD"

    def test_invalid_arn_too_few_parts(self):
        with pytest.raises(ValueError, match="Invalid ARN format"):
            parse_arn("arn:aws:lambda:us-west-2")

    def test_invalid_arn_no_prefix(self):
        with pytest.raises(ValueError, match="Invalid ARN format"):
            parse_arn("not:an:arn:at:all:really")

    def test_empty_string(self):
        with pytest.raises(ValueError, match="Invalid ARN format"):
            parse_arn("")


# ---------------------------------------------------------------------------
# extract_region
# ---------------------------------------------------------------------------

class TestExtractRegion:
    def test_extracts_region(self):
        arn = "arn:aws:connect:ap-northeast-1:123456789012:instance/abc-123"
        assert extract_region(arn) == "ap-northeast-1"

    def test_empty_region_raises(self):
        arn = "arn:aws:iam::123456789012:role/my-role"
        with pytest.raises(ValueError, match="no region component"):
            extract_region(arn)

    def test_invalid_arn_raises(self):
        with pytest.raises(ValueError, match="Invalid ARN format"):
            extract_region("garbage")


# ---------------------------------------------------------------------------
# resolve_target_region
# ---------------------------------------------------------------------------

class TestResolveTargetRegion:
    @pytest.mark.parametrize(
        "source, expected_target",
        [
            ("us-west-2", "us-east-1"),
            ("us-east-1", "us-west-2"),
            ("eu-west-2", "eu-central-1"),
            ("eu-central-1", "eu-west-2"),
            ("ap-northeast-1", "ap-northeast-3"),
        ],
    )
    def test_supported_pairs(self, source, expected_target):
        assert resolve_target_region(source) == expected_target

    def test_unsupported_region_raises(self):
        with pytest.raises(ValueError, match="Unsupported ACGR region"):
            resolve_target_region("ap-southeast-1")

    def test_error_lists_supported_pairs(self):
        with pytest.raises(ValueError, match="us-west-2"):
            resolve_target_region("us-west-1")

    def test_all_pairs_in_constant(self):
        """Verify the mapping has exactly the 5 expected entries."""
        assert len(ACGR_REGION_PAIRS) == 5


# ---------------------------------------------------------------------------
# rewrite_arn
# ---------------------------------------------------------------------------

class TestRewriteArn:
    def test_rewrites_region(self):
        original = "arn:aws:lambda:us-west-2:123456789012:function:my-func"
        result = rewrite_arn(original, "us-east-1")
        assert result == "arn:aws:lambda:us-east-1:123456789012:function:my-func"

    def test_preserves_all_other_parts(self):
        original = "arn:aws:dynamodb:eu-west-2:999999999999:table/Orders"
        result = rewrite_arn(original, "eu-central-1")
        parsed = parse_arn(result)
        assert parsed["partition"] == "aws"
        assert parsed["service"] == "dynamodb"
        assert parsed["region"] == "eu-central-1"
        assert parsed["account"] == "999999999999"
        assert parsed["resource"] == "table/Orders"

    def test_resource_with_colons_preserved(self):
        original = "arn:aws:lambda:us-east-1:123456789012:function:f:PROD"
        result = rewrite_arn(original, "us-west-2")
        assert result.endswith("function:f:PROD")
        assert ":us-west-2:" in result

    def test_invalid_arn_raises(self):
        with pytest.raises(ValueError, match="Invalid ARN format"):
            rewrite_arn("not-an-arn", "us-east-1")

    def test_round_trip_with_region_pairs(self):
        """Rewriting source->target->source should return the original ARN."""
        original = "arn:aws:kinesis:us-west-2:123456789012:stream/my-stream"
        target = rewrite_arn(original, "us-east-1")
        back = rewrite_arn(target, "us-west-2")
        assert back == original
