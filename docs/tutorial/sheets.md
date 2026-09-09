# Sheet tables tutorial

Bind a **Google Sheet worksheet** as a relational table while preserving columns your team added outside GDM.

## 1. Define a model

```python
--8<-- "models_task.py"
```

| Class keyword | Purpose |
| --- | --- |
| `storage="sheet"` | One worksheet per model |
| `source="Tasks/Q1"` | Spreadsheet path hint (resolved to ID before writes) |
| `mapping="partial"` | Preserve unknown columns (default) |

## 2. Connect and bind

```python
from gdm import GDM, ResourceKind

client = GDM.open(root_folder_id="YOUR_ROOT_FOLDER_ID")
client.bind(
    Task,
    resource_id="YOUR_SPREADSHEET_ID",
    kind=ResourceKind.SPREADSHEET,
    worksheet="Tasks",
)
```

## 3. Query metadata and rows

```python
--8<-- "query_sheet.py"
```

Field comparisons build a backend-independent expression tree. Sheet reads fetch **bounded ranges** and evaluate predicates locally.

```python
from gdm import select

stmt = (
    select(Task)
    .where(Task.status == "open")
    .where(Task.assignee != None)  # noqa: E711
    .order_by(Task.title)
    .limit(50)
)
rows = session.exec(stmt).all()
```

Set `scan_limit` on the client session when scanning large worksheets:

```python
with client.session(scan_limit=500) as session:
    ...
```

## 4. Create, update, delete

```python
--8<-- "crud_create.py"
```

```python
task = session.get(Task, "task-001")
task.status = "done"
session.add(task)
session.commit()  # re-reads source; raises ConflictError on drift
```

Sheet row deletes remove the row from the worksheet. Collection deletes trash the Drive file instead—see [Drive collections](collection.md).

## 5. Relationships and Drive file IDs

Foreign keys are ordinary cells. Link sheet rows to collection objects by storing **Drive file IDs**:

```python
class Order(GDMModel, storage="sheet"):
    id: str = Field(primary_key=True)
    asset_id: str = Field(foreign_key="CampaignAsset.drive_id")
```

Resolve client-side with batched prefetch—no server-side joins:

```python
orders = session.exec(select(Order).limit(100)).all()
assets = session.prefetch(CampaignAsset, [o.asset_id for o in orders])
```

See [Relationships](../storage/relationships.md).

## 6. Adopt an existing sheet

```console
gdm explore --root-id YOUR_ROOT_FOLDER_ID
gdm infer file sample_rows.csv --model-name Inventory
```

Review the generated model, then bind the spreadsheet ID and worksheet with
`client.bind(..., kind=ResourceKind.SPREADSHEET)` as shown in step 2.
`gdm adopt` is intentionally folder-only; see
[Adopt an existing spreadsheet](../adoption/existing-sheet.md).

## Next

- [Sheets as tables](../storage/sheets.md)
- [Indexes](../performance/indexes.md)
- [Migrations](../migrations/index.md)
