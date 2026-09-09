"""Adopt an existing inventory worksheet."""

from gdm import Field, GDMModel


class Inventory(
    GDMModel,
    storage="sheet",
    mapping="partial",
    source="Inventory/Q4 worksheet",
):
    sku: str = Field(primary_key=True, source="SKU Code")
    quantity: int = Field(source="Qty")
    warehouse: str | None = Field(default=None, source="Location")
