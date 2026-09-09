# Relationships

GDM supports **typed links** between models using foreign keys stored as ordinary cell
values or **Drive file IDs**.

## Sheet foreign keys

```python
from gdm import Field, GDMModel


class Customer(GDMModel, storage="sheet", mapping="partial"):
    id: str = Field(primary_key=True)
    name: str


class Order(GDMModel, storage="sheet", mapping="partial"):
    id: str = Field(primary_key=True)
    customer_id: str = Field(foreign_key="Customer.id")
    total: float
```

The `customer_id` cell contains the customer's primary key string.

## Link to Drive collection objects

Collection identity is the Drive file ID. Reference it from sheet rows:

```python
from gdm import DriveObjectMixin, Field, GDMModel


class CampaignAsset(
    GDMModel,
    DriveObjectMixin,
    storage="collection",
    mapping="partial",
):
    campaign: str | None = Field(default=None, metadata="campaign")


class CampaignTask(GDMModel, storage="sheet"):
    id: str = Field(primary_key=True)
    asset_id: str = Field(foreign_key="CampaignAsset.drive_id")
    title: str
```

Or link between collection metadata fields when both sides are modeled.

## Resolution

Relationships resolve **client-side** with batched prefetch:

```python
orders = session.exec(select(Order).limit(100)).all()
customers = session.prefetch(Customer, [o.customer_id for o in orders])

campaign_tasks = session.exec(select(CampaignTask).limit(100)).all()
assets = session.prefetch(
    CampaignAsset,
    [task.asset_id for task in campaign_tasks],
)

for order in orders:
    customer = customers.get(order.customer_id)
    if customer is not None:
        print(order.id, customer.name)
```

`prefetch()` returns a dictionary keyed by each related object's primary key
(Drive file ID for collection objects). Missing keys are omitted.

There is:

- no storage-enforced referential integrity
- no cascade delete in the sheet or folder
- no SQL-style join pushdown

!!! platform-limit "No joins in Drive"
    Joins and relationship filters load related records in batches. Deep graphs can be
    quota-expensive—denormalize or move to a database when needed.

## Related

- [Sheet tables tutorial](../tutorial/sheets.md)
- [Indexes](../performance/indexes.md)
