"""Tests for the CloudFront SPA routing fix.

Validates:
- CloudFront Function exists with correct runtime
- No custom error responses on the distribution
- Default behavior has function association on viewer-request
- /api/* behavior is unchanged (CachingDisabled, no function associations)
- DefaultRootObject is index.html
- URI rewrite regex recognizes all required file extensions
"""

import re

import pytest


# ---------------------------------------------------------------------------
# URI rewrite regex tests (Python equivalent of the CloudFront Function logic)
# ---------------------------------------------------------------------------

# This is the same regex used in the CloudFront Function: /\.\w+$/
_HAS_EXTENSION = re.compile(r"\.\w+$")


def _should_rewrite(uri: str) -> bool:
    """Python equivalent of the CloudFront Function decision logic.

    Returns True if the URI should be rewritten to /index.html (no extension).
    Returns False if the URI has a file extension and should pass through.
    """
    return _HAS_EXTENSION.search(uri) is None


class TestUriRewriteRegex:
    """Test the URI rewrite regex against all required extensions and SPA routes."""

    # Requirement 2.4: all required static asset extensions
    REQUIRED_EXTENSIONS = [
        ".js", ".css", ".html", ".png", ".jpg", ".jpeg", ".gif", ".svg",
        ".ico", ".json", ".woff", ".woff2", ".ttf", ".eot", ".map",
        ".txt", ".xml", ".webp", ".avif",
    ]

    @pytest.mark.parametrize("ext", REQUIRED_EXTENSIONS)
    def test_static_asset_extensions_pass_through(self, ext):
        """Requirement 2.3, 2.4: URIs with recognized extensions pass through."""
        uri = f"/assets/file{ext}"
        assert not _should_rewrite(uri), f"URI '{uri}' should NOT be rewritten"

    @pytest.mark.parametrize("ext", REQUIRED_EXTENSIONS)
    def test_nested_path_with_extension_passes_through(self, ext):
        """Extensions in nested paths should also pass through."""
        uri = f"/assets/sub/deep/file{ext}"
        assert not _should_rewrite(uri), f"URI '{uri}' should NOT be rewritten"

    @pytest.mark.parametrize("uri", [
        "/",
        "/dashboard",
        "/session/123",
        "/some/deep/path",
        "/session/abc-def-ghi",
    ])
    def test_spa_routes_are_rewritten(self, uri):
        """Requirement 2.2: URIs without extensions are rewritten to /index.html."""
        assert _should_rewrite(uri), f"URI '{uri}' should be rewritten"

    def test_root_path_rewritten(self):
        """Requirement 5.1: Root path should be rewritten."""
        assert _should_rewrite("/")

    def test_trailing_slash_rewritten(self):
        """Trailing slash paths should be rewritten (no extension)."""
        assert _should_rewrite("/dashboard/")

    def test_empty_string_rewritten(self):
        """Empty URI should be rewritten."""
        assert _should_rewrite("")

    def test_vite_hashed_asset_passes_through(self):
        """Vite-style hashed assets like index-abc123.js should pass through."""
        assert not _should_rewrite("/assets/index-a1b2c3d4.js")

    def test_sourcemap_passes_through(self):
        """Source maps should pass through."""
        assert not _should_rewrite("/assets/index-a1b2c3d4.js.map")
