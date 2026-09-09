"""Tests for Docs/Slides google_api wrappers."""

from __future__ import annotations

from unittest.mock import MagicMock

from gdm.google_api import (
    ExecutingDocsService,
    ExecutingSlidesService,
    wrap_docs_service,
    wrap_slides_service,
)


def test_wrap_docs_service_executes() -> None:
    raw = MagicMock()
    doc_resource = MagicMock()
    raw.documents.return_value = doc_resource
    doc_resource.get.return_value.execute.return_value = {"documentId": "d1"}
    service = wrap_docs_service(raw)
    result = service.documents().get(documentId="d1")
    assert result["documentId"] == "d1"


def test_wrap_slides_service_batch_update() -> None:
    raw = MagicMock()
    pres = MagicMock()
    raw.presentations.return_value = pres
    pres.batchUpdate.return_value.execute.return_value = {"presentationId": "p1"}
    service = wrap_slides_service(raw)
    result = service.presentations().batchUpdate(
        presentationId="p1",
        body={"requests": []},
    )
    assert result["presentationId"] == "p1"


def test_executing_docs_and_slides_constructors() -> None:
    raw = MagicMock()
    raw.documents.return_value = MagicMock()
    raw.presentations.return_value = MagicMock()
    assert isinstance(ExecutingDocsService(raw).documents(), object)
    assert isinstance(ExecutingSlidesService(raw).presentations(), object)
