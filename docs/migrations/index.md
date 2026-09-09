# Migrations

GDM migrations are **ordered, resumable operations** with deterministic IDs and
checkpoints in the local project directory
`<project>/.gdm/migrations/`—implemented in **`gdm.migrations`**, not Alembic
or SQL DDL.

```console
gdm --project-dir . migrate plan --model Task --resource-id RESOURCE_ID
gdm --project-dir . --apply migrate apply --model Task --resource-id RESOURCE_ID
gdm --project-dir . migrate rollback --model Task --resource-id RESOURCE_ID
```

Before planning, save the current code model snapshot (normally from a small
application maintenance script):

```python
from gdm.schema import SchemaRegistry, build_model_snapshot
from my_app.models import Task

SchemaRegistry(".gdm/schemas").save(build_model_snapshot(Task))
```

The first successful apply copies the current snapshot to the `applied` slot.
Subsequent model changes produce a diff between those two files.

## Supported operations (additive default)

| Operation family | Example |
| --- | --- |
| Add field/header | New column with validated default |
| Rename field | Rename the backing header; keep a Pydantic alias during rollout if needed |
| Transform values | Supported scalar rewrite with row-specific validation results |
| Backfill IDs/versions | Add `_gdm_id` to legacy sheet |
| Collection metadata | Add/rename metadata columns without touching native bodies |
| Rebuild index | Refresh sidecar index files |

Content moves and native body rewrites require **explicit, separately named**
APIs (`move`, `content.upload`, …). Automatic MIME conversion is not shipped in
v0.1: a `convert_mime` migration is deliberately blocked so callers must export,
create the destination object, verify it, and then move/trash explicitly.

## Destructive changes

Dropping fields, deleting source values, and replacing whole worksheets require
**`--allow-destructive`** on apply. MIME conversion remains unsupported even with
that flag. Default plans are **additive only**.

## Inspect before apply

Every plan can be inspected without mutation. Dry-run application reports operation
boundaries and performs **no Drive write calls or native-body reads**. Transform failures
include row identifiers during apply.

## Resumable application

Each completed operation is recorded in local project metadata. Failed runs resume by
skipping completed steps. Because Drive is not transactional, inspect partially changed
rows before retrying an operation that itself reported row-level errors.

!!! platform-limit "No transactional rollback"
    Rollback exists only when a migration declares a reverse operation. GDM never promises
    transactional rollback across Drive resources.

## Python API

```python
from gdm.migrations import MigrationEngine, MigrationPlanner
from gdm.schema import SchemaRegistry

registry = SchemaRegistry("/path/to/.gdm/schemas")
applied = registry.load("Task", applied=True)
current = registry.load("Task", applied=False)

planner = MigrationPlanner()
plan = planner.plan_from_snapshots(
    model_name="Task",
    current=applied or current,
    target=current,
    resource_id="SPREADSHEET_OR_FOLDER_ID",
    dry_run=True,
)
for op in plan.operations:
    print(op.kind.value, op.summary())

target = client.migration_target(
    indexes_dir=".gdm/indexes",
    checkpoint_dir=".gdm/migrations",
)
engine = MigrationEngine(target)
report = engine.apply(plan)
```

Here `client` is an open `GDMClient`. The CLI builds the same production target
when you pass `--apply`; tests may inject `migration_target_factory` instead.

## Related

- [Schema drift](drift.md)
- [CLI](../cli/index.md)
- [Design: migrations](../design.md#decision-6-drift-aware-forward-migrations)
