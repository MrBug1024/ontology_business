"""Quoted keys in pasted JSON are the same secrets as key=value text."""
from __future__ import annotations

import pytest

from app.services.release_service import safe_snapshot_content


@pytest.mark.parametrize("content", [
    '{"password":"synthetic-private-value"}',
    '{ "api_key" : "synthetic-private-value" }',
    '{"nested":{"access_token":"synthetic-private-value"}}',
    "{'authorization': 'synthetic-private-value'}",
    "password=synthetic-private-value",
    "token:synthetic-private-value",
])
def test_string_containing_credential_assignment_is_redacted(content):
    result = safe_snapshot_content({"content": content})
    assert result != {"content": content}
    assert "synthetic-private-value" not in str(result)


@pytest.mark.parametrize("content", [
    '{"password_policy":"minimum length"}',
    '{"password_reset_required":true}',
    'Explain the "password" field and who is allowed to reset it.',
    '{"stage":"review","case_number":"example-42"}',
])
def test_non_secret_business_text_keeps_original_content(content):
    assert safe_snapshot_content({"content": content}) == {"content": content}
