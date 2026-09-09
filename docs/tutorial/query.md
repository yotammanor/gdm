# Query data

Query documentation moved to:

- **Collection metadata queries (no body fetch):** [Drive collections tutorial — metadata reconciliation](collection.md#8-metadata-reconciliation) and [quickstart](../index.md)
- **Sheet row queries:** [Sheet tables tutorial — §3](sheets.md#3-query-metadata-and-rows)

Core API:

```python
from gdm import select

stmt = select(Model).where(Model.field == value).limit(50)
rows = session.exec(stmt).all()
```

See [API reference](../api/index.md#quick-reference) and [Indexes](../performance/indexes.md).
