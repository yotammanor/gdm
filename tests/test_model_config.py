"""Tests for model configuration keywords."""

from __future__ import annotations

import pytest

from gdm.errors import InvalidForeignKeyError, InvalidModelConfigError
from gdm.fields import Field
from gdm.model import GDMModel


def test_table_alias_sets_sheet_storage() -> None:
    class Row(GDMModel, table=True):
        name: str

    assert Row.gdm_storage() == "sheet"


def test_codec_requires_file_storage() -> None:
    with pytest.raises(InvalidModelConfigError, match="codec="):

        class Bad(GDMModel, storage="sheet", codec="json"):
            name: str


def test_invalid_codec_rejected() -> None:
    with pytest.raises(InvalidModelConfigError, match="Unsupported codec"):

        class Bad(GDMModel, storage="file", codec="xml"):  # type: ignore[arg-type]
            name: str


def test_foreign_key_format_validated() -> None:
    with pytest.raises(InvalidForeignKeyError):

        class Order(GDMModel, storage="sheet"):
            customer_id: str = Field(foreign_key="Customer")

    class OrderOk(GDMModel, storage="sheet"):
        customer_id: str = Field(foreign_key="Customer.id")

    assert OrderOk.model_fields["customer_id"] is not None
