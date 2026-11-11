"""Shared pytest fixtures for CDK stack property tests.

The stack module lives one directory above this file (``infra/stack.py``), so
we inject the parent directory onto ``sys.path`` to make ``import stack``
resolve the same way ``app.py`` does.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from aws_cdk import App
from aws_cdk.assertions import Template

from stack import ConnectAcgrReplicatorStack


@pytest.fixture(scope="session")
def synthesized_template() -> Template:
    """Synthesize the stack once per test session and return its Template."""
    app = App()
    stack = ConnectAcgrReplicatorStack(app, "TestStack")
    return Template.from_stack(stack)


@pytest.fixture(scope="session")
def template_json(synthesized_template: Template) -> dict:
    """Return the synthesized template as a plain dict."""
    return synthesized_template.to_json()
