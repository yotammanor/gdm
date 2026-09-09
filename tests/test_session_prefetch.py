"""Tests for session prefetch and scan limits."""

from __future__ import annotations

import pytest

from gdm.backends.memory import MemoryBackend
from gdm.errors import ScanLimitExceeded
from gdm.fields import Field
from gdm.model import GDMModel
from gdm.session import Session


class Customer(GDMModel, storage="sheet", source="customers"):
    id: str = Field(primary_key=True)
    name: str


class Order(GDMModel, storage="sheet", source="orders"):
    id: str = Field(primary_key=True)
    customer_id: str = Field(foreign_key="Customer.id")
    total: int


def test_prefetch_loads_related_records() -> None:
    backend = MemoryBackend()
    backend.seed_record(Customer, {"id": "c1", "name": "Acme"})
    backend.seed_record(Customer, {"id": "c2", "name": "Beta"})
    with Session(backend) as session:
        found = session.prefetch(Customer, ["c2", "c1", "c2"])
    assert set(found) == {"c1", "c2"}
    assert found["c1"].name == "Acme"


def test_prefetch_scan_limit() -> None:
    backend = MemoryBackend()
    for index in range(5):
        backend.seed_record(Customer, {"id": f"c{index}", "name": f"n{index}"})
    with Session(backend, scan_limit=3) as session:
        with pytest.raises(ScanLimitExceeded):
            session.prefetch(Customer, ["c4"])
