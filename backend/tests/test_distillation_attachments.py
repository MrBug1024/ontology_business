from __future__ import annotations

from io import BytesIO
from pathlib import Path
import subprocess
from tempfile import NamedTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.distillation_conversation_schemas import TurnCreate
from app.services import distillation_attachment_parser as parser


def staged(content: bytes, suffix=".txt") -> Path:
    with NamedTemporaryFile(prefix="ontology-upload-", suffix=suffix, delete=False) as file:
        file.write(content)
        return Path(file.name)


def test_real_bounded_parser_returns_text_and_blocks_quoted_credentials():
    for content, accepted in ((b"A request was recorded without a resolution.", True),
        (b'{"password":"synthetic-private-value","stage":"recorded"}', False)):
        path = staged(content)
        try:
            if accepted:
                result = parser.parse_staged(path, "evidence.txt", "text/plain")
                assert result.text == content.decode()
                assert result.media_type == "text/plain"
            else:
                with pytest.raises(HTTPException) as failure:
                    parser.parse_staged(path, "evidence.txt", "text/plain")
                assert failure.value.status_code == 422
                assert "synthetic-private-value" not in str(failure.value)
        finally:
            path.unlink()


def test_office_zip_bomb_is_rejected_before_document_parsing():
    stream = BytesIO()
    with ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "A" * (2 * 1024 * 1024))
    path = staged(stream.getvalue(), ".docx")
    try:
        with pytest.raises(HTTPException) as failure:
            parser.parse_staged(path, "large.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        assert failure.value.status_code == 422
    finally:
        path.unlink()


def test_parser_timeout_has_safe_error_and_subprocess_deadline(monkeypatch):
    path = staged(b"Timeout example")
    def timeout(command, **kwargs):
        assert kwargs["timeout"] == parser.MAX_PARSE_SECONDS
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert not any("api_key" in argument for argument in command)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
    monkeypatch.setattr(subprocess, "run", timeout)
    try:
        with pytest.raises(HTTPException) as failure:
            parser.parse_staged(path, "evidence.txt", "text/plain")
        assert failure.value.status_code == 422
    finally:
        path.unlink()


def test_turn_requires_text_or_explicit_inputs_and_bounds_duplicate_attachments():
    assert TurnCreate(request_id="attachment_only", expected_revision=1, attachment_ids=["a"]).message == ""
    with pytest.raises(ValidationError):
        TurnCreate(request_id="empty", expected_revision=1)
    with pytest.raises(ValidationError):
        TurnCreate(request_id="duplicate", expected_revision=1, attachment_ids=["a", "a"])
    with pytest.raises(ValidationError):
        TurnCreate(request_id="many", expected_revision=1, attachment_ids=[str(index) for index in range(6)])
