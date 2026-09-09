"""Typer CLI for GDM inspection, inference, migrations, and indexes."""

# ruff: noqa: B008 - Typer uses call expressions in parameter defaults by design.

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from gdm.explore import Explorer, inventory_to_dict
from gdm.indexes import (
    IndexMaintainer,
    IndexStore,
    StalePolicy,
    compute_collection_fingerprints,
)
from gdm.infer import (
    generate_model_code,
    infer_from_collection_inventory,
    infer_from_json_text,
    infer_from_sheet_headers,
    infer_from_text_lines,
)
from gdm.migrations import (
    MigrationCheckpoint,
    MigrationEngine,
    MigrationPlan,
    MigrationPlanner,
    load_checkpoint,
    load_checkpoint_plan,
    serialize_checkpoint,
)
from gdm.schema import DriftSeverity, SchemaRegistry, detect_drift
from gdm.tooling.adopt import AdoptPlanner
from gdm.tooling.doctor import doctor_payload, run_collection_doctor

app = typer.Typer(
    name="gdm",
    help="Google Drive Model — inspect, infer, migrate, and index Drive-backed data.",
    no_args_is_help=True,
)
migrate_app = typer.Typer(help="Plan and apply schema migrations.")
index_app = typer.Typer(help="Sidecar index maintenance.")
auth_app = typer.Typer(help="Authentication helpers.")
infer_app = typer.Typer(help="Infer GDM models from Drive metadata or local samples.")

app.add_typer(migrate_app, name="migrate")
app.add_typer(index_app, name="index")
app.add_typer(auth_app, name="auth")
app.add_typer(infer_app, name="infer")

console = Console(stderr=True)


@dataclass
class CLIContext:
    """Runtime dependencies for testable CLI wiring."""

    project_dir: Path
    dry_run: bool = True
    explorer_factory: Callable[[], Explorer] | None = None
    schema_registry_factory: Callable[[], SchemaRegistry] | None = None
    index_store_factory: Callable[[], IndexStore] | None = None
    migration_target_factory: Callable[[], Any] | None = None
    auth_login_factory: Callable[[], int] | None = None
    adopt_binder_factory: Callable[[], Callable[[Any], dict[str, Any]]] | None = None
    metadata_store_factory: Callable[[], Any] | None = None
    api_call_log: list[str] = field(default_factory=list)


def _default_project_dir() -> Path:
    return Path(os.environ.get("GDM_PROJECT_DIR", ".")).resolve()


def _get_context(ctx: typer.Context) -> CLIContext:
    context = ctx.obj
    if isinstance(context, CLIContext):
        return context
    return CLIContext(project_dir=_default_project_dir())


def _gdm_dir(project_dir: Path) -> Path:
    return project_dir / ".gdm"


def _schemas_dir(project_dir: Path) -> Path:
    return _gdm_dir(project_dir) / "schemas"


def _migrations_dir(project_dir: Path) -> Path:
    return _gdm_dir(project_dir) / "migrations"


def _default_checkpoint_path(project_dir: Path, model_name: str) -> Path:
    return _migrations_dir(project_dir) / f"{model_name}.checkpoint.json"


def _load_local_manifest(project_dir: Path) -> dict[str, Any]:
    manifest_path = _gdm_dir(project_dir) / "manifest.json"
    if not manifest_path.is_file():
        console.print(
            "[red]Missing .gdm/manifest.json. "
            "Run `gdm init --root-id FOLDER_ID` first.[/]"
        )
        raise typer.Exit(code=1)
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        console.print(f"[red]Invalid manifest.json: {exc}[/]")
        raise typer.Exit(code=1) from exc
    if not isinstance(data, dict):
        console.print("[red]manifest.json must be a JSON object.[/]")
        raise typer.Exit(code=1)
    return data


def _build_default_migration_target(
    context: CLIContext,
    *,
    model_name: str,
    resource_id: str,
) -> Any:
    client = _build_default_client(context)
    from gdm.migration_target import build_production_migration_target

    schemas = SchemaRegistry(str(_schemas_dir(context.project_dir)))

    def field_lookup(model: str, field: str) -> str | None:
        snap = schemas.load(model, applied=False)
        if snap is None:
            return None
        for meta in snap.fields:
            if meta.name == field:
                return meta.source_name
        return None

    def field_target_lookup(model: str, field: str) -> tuple[str, str] | None:
        snap = schemas.load(model, applied=False)
        if snap is None:
            return None
        for meta in snap.fields:
            if meta.name == field:
                return meta.target_kind, meta.source_name
        return None

    _migrations_dir(context.project_dir).mkdir(parents=True, exist_ok=True)
    return build_production_migration_target(
        client,
        bindings={model_name: resource_id},
        indexes_dir=str(_indexes_dir(context.project_dir)),
        checkpoint_dir=_migrations_dir(context.project_dir),
        schema_field_lookup=field_lookup,
        schema_field_target_lookup=field_target_lookup,
    )


def _build_default_client(
    context: CLIContext,
    *,
    adoption_mode: bool = False,
) -> Any:
    manifest = _load_local_manifest(context.project_dir)
    root_id = manifest.get("root_folder_id")
    if not root_id:
        console.print("[red]manifest.json is missing root_folder_id.[/]")
        raise typer.Exit(code=1)

    from gdm.client import GDMClient

    try:
        return GDMClient.connect(
            root_folder_id=str(root_id),
            use_adc=True,
            indexes_dir=str(_indexes_dir(context.project_dir)),
            adoption_mode=adoption_mode,
        )
    except Exception as exc:
        console.print(
            "[red]Failed to connect to Drive project. "
            "Run `gdm auth login` or configure ADC, then retry.[/]"
        )
        console.print(f"[dim]{exc}[/]")
        raise typer.Exit(code=1) from exc


def _build_default_adopt_binder(
    context: CLIContext,
    *,
    metadata_backend: str,
    adoption_mode: bool = False,
) -> Callable[[Any], dict[str, Any]]:
    client = _build_default_client(context, adoption_mode=adoption_mode)

    def bind(plan: Any) -> dict[str, Any]:
        from gdm.collection.constants import (
            FOLDER_MIME,
            METADATA_BACKEND_JSON,
            METADATA_BACKEND_SHEETS,
        )
        from gdm.errors import GDMError
        from gdm.migration_target import build_production_migration_target

        metadata = client.drive.get_metadata(plan.folder_id)
        if metadata.mime_type != FOLDER_MIME:
            raise GDMError(f"Resource {plan.folder_id!r} is not a Drive folder.")
        client.project.assert_resource_belongs_to_project(
            resource_id=plan.folder_id,
            app_properties=metadata.app_properties,
        )
        target = build_production_migration_target(
            client,
            indexes_dir=str(_indexes_dir(context.project_dir)),
        )
        target.bind_collection(
            plan.model_name,
            folder_id=plan.folder_id,
            collection_path=plan.collection_path,
        )
        if metadata_backend == METADATA_BACKEND_JSON:
            client.project.ensure_collection_json_metadata_binding(plan.model_name)
        elif metadata_backend == METADATA_BACKEND_SHEETS:
            client.project.ensure_collection_metadata_binding(plan.model_name)
        else:  # pragma: no cover - validated by the CLI command
            raise GDMError(f"Unsupported metadata backend {metadata_backend!r}.")
        entry = client.project.get_registered(plan.model_name)
        return entry.to_dict()

    return bind


def _existing_collection_metadata_store(
    client: Any,
    model_name: str,
) -> Any | None:
    from gdm.collection.constants import METADATA_BACKEND_JSON
    from gdm.collection.metadata import (
        DriveJsonCollectionMetadataStore,
        SheetsCollectionMetadataStore,
    )

    entry = client.project.registry.get(model_name)
    if entry is None:
        return None
    backend = client.project.metadata_backend_for(model_name)
    if backend == METADATA_BACKEND_JSON:
        file_id = entry.extra.get("metadata_json_file_id")
        if not file_id:
            return None
        return DriveJsonCollectionMetadataStore(
            client.project.drive,
            file_id=str(file_id),
        )
    spreadsheet_id = entry.extra.get("metadata_spreadsheet_id")
    if not spreadsheet_id:
        return None
    return SheetsCollectionMetadataStore(
        client.sheets.service.spreadsheets(),
        spreadsheet_id=str(spreadsheet_id),
        worksheet=str(entry.extra.get("metadata_worksheet") or "metadata"),
    )


def _indexes_dir(project_dir: Path) -> Path:
    return _gdm_dir(project_dir) / "indexes"


def _persist_checkpoint(
    path: Path, checkpoint: MigrationCheckpoint, plan: MigrationPlan | None = None
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize_checkpoint(checkpoint, plan) + "\n", encoding="utf-8")


@app.callback()
def main(
    ctx: typer.Context,
    project_dir: Path = typer.Option(
        None,
        "--project-dir",
        help="Local project directory containing .gdm metadata.",
        envvar="GDM_PROJECT_DIR",
    ),
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--apply",
        help="Default to inspection-only mode; use --apply to mutate.",
    ),
) -> None:
    """Global CLI options."""
    resolved = project_dir.resolve() if project_dir else _default_project_dir()
    existing = ctx.obj
    if isinstance(existing, CLIContext):
        existing.project_dir = resolved
        existing.dry_run = dry_run
        ctx.obj = existing
    else:
        ctx.obj = CLIContext(project_dir=resolved, dry_run=dry_run)


@app.command("init")
def init_cmd(
    ctx: typer.Context,
    root_id: str = typer.Option(..., help="Drive root folder ID for the project."),
    name: str = typer.Option("default", help="Project display name."),
    force: bool = typer.Option(False, help="Overwrite local metadata scaffolding."),
    remote: bool = typer.Option(
        False,
        "--remote/--local-only",
        help="When combined with --apply, create/open the remote Drive project.",
    ),
) -> None:
    """Initialize local .gdm project scaffolding (metadata only by default)."""
    context = _get_context(ctx)
    gdm_dir = _gdm_dir(context.project_dir)
    if gdm_dir.exists() and not force:
        console.print(
            "[yellow]Project metadata already exists; use --force to recreate.[/]"
        )
        raise typer.Exit(code=0)

    for sub in ("schemas", "migrations", "indexes"):
        (gdm_dir / sub).mkdir(parents=True, exist_ok=True)

    manifest = {
        "format_version": "1",
        "name": name,
        "root_folder_id": root_id,
    }
    manifest_path = gdm_dir / "manifest.json"
    registry_path = gdm_dir / "registry.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if not registry_path.exists():
        registry_path.write_text('{"entries": {}}\n', encoding="utf-8")

    console.print(f"[green]Initialized GDM project metadata at {gdm_dir}[/]")
    if remote and not context.dry_run:
        from gdm.client import GDMClient
        from gdm.errors import NotFoundError

        try:
            client = GDMClient.open(root_folder_id=root_id, use_adc=True)
        except NotFoundError:
            client = GDMClient.create(root_folder_id=root_id, use_adc=True)
        manifest["project_id"] = str(client.project.project_id)
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        remote_registry = getattr(client.project, "registry", None)
        if remote_registry is not None:
            registry_path.write_text(
                json.dumps(
                    remote_registry.to_dict(),
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        console.print(
            f"[green]Remote project initialized under root {root_id} "
            f"({client.project.project_id}).[/]"
        )
    elif remote:
        console.print(
            "[yellow]Remote init requested but --dry-run is active; "
            "pass --apply to mutate Drive.[/]"
        )
    else:
        console.print(
            "[dim]Drive root binding is local metadata only; "
            "no remote mutation performed.[/]"
        )


@app.command("explore")
def explore_cmd(
    ctx: typer.Context,
    root_id: str = typer.Option(..., help="Drive folder ID to explore."),
    max_resources: int = typer.Option(1000, help="Maximum resources to inventory."),
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON."
    ),
) -> None:
    """Inventory a Drive root and report collection candidates without content reads."""
    context = _get_context(ctx)
    explorer = (
        context.explorer_factory()
        if context.explorer_factory
        else _build_default_explorer(max_resources)
    )
    report = explorer.explore(root_id)
    if json_output:
        typer.echo(json.dumps(inventory_to_dict(report), indent=2, sort_keys=True))
        return

    inv = report.inventory
    table = Table(title=f"Explore {root_id}")
    table.add_column("Category")
    table.add_column("MIME")
    table.add_column("Count")
    table.add_column("Total size")
    for candidate in inv.type_candidates:
        table.add_row(
            candidate.category.value,
            candidate.mime_type,
            str(candidate.count),
            str(candidate.total_size),
        )
    console.print(table)
    console.print(
        f"[dim]Direct children: {inv.direct_count}; nested: {inv.nested_count}; "
        f"zero content reads: {inv.zero_content_reads}[/]"
    )
    if inv.collection_candidates:
        coll = Table(title="Collection candidates")
        coll.add_column("Folder ID")
        coll.add_column("Name")
        coll.add_column("Direct")
        coll.add_column("Nested")
        coll.add_column("GDM")
        for c in inv.collection_candidates[:10]:
            coll.add_row(
                c.folder_id,
                c.name,
                str(c.direct_child_count),
                str(c.nested_descendant_count),
                "yes" if c.has_gdm_metadata else "no",
            )
        console.print(coll)
    for warning in report.warnings:
        console.print(f"[yellow]{warning}[/]")
    for note in report.runtime_integration_notes:
        console.print(f"[blue]{note}[/]")
    console.print(f"[dim]{inv.content_read_guarantee}[/]")


@infer_app.command("collection")
def infer_collection_cmd(
    ctx: typer.Context,
    root_id: str | None = typer.Option(None, help="Drive root folder ID to explore."),
    folder_id: str | None = typer.Option(
        None, help="Specific folder ID to infer as a collection."
    ),
    model_name: str = typer.Option(
        "InferredCollection", help="Generated model class name."
    ),
    collection_path: str | None = typer.Option(
        None, help="Collection path hint for generated model."
    ),
    output: Path | None = typer.Option(
        None, help="Optional path to write generated code."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit binding JSON."),
) -> None:
    """Infer a collection model from Drive metadata without writes or content reads."""
    context = _get_context(ctx)
    target_folder = folder_id or root_id
    if target_folder is None:
        console.print("[red]Pass --folder-id or --root-id.[/]")
        raise typer.Exit(code=1)

    explorer = (
        context.explorer_factory()
        if context.explorer_factory
        else _build_default_explorer(1000)
    )
    explore_root = root_id or target_folder
    report = explorer.explore(explore_root)
    result = infer_from_collection_inventory(
        report.inventory.resources,
        folder_id=target_folder,
        model_name=model_name,
        collection_path=collection_path,
    )
    code = generate_model_code(result)
    binding = result.binding
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "binding": {
                        "collection_path": binding.collection_path,
                        "folder_id": binding.folder_id,
                        "mapping_mode": binding.mapping_mode,
                        "mime_patterns": list(binding.mime_patterns),
                        "primary_key": binding.primary_key,
                        "storage": binding.storage,
                    },
                    "model_name": result.model_name,
                    "source_kind": result.source_kind,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        console.print("[bold]Binding proposal (no writes)[/]")
        console.print(f"  storage: {binding.storage}")
        console.print(f"  folder_id: {binding.folder_id}")
        console.print(f"  collection_path: {binding.collection_path}")
        console.print(f"  mime_patterns: {', '.join(binding.mime_patterns) or '-'}")
        console.print("\n[bold]Generated model[/]")
        typer.echo(code)
    if output is not None:
        output.write_text(code, encoding="utf-8")
        console.print(f"[green]Wrote {output}[/]")


@infer_app.command("file")
def infer_file_cmd(
    ctx: typer.Context,
    source: Path = typer.Argument(..., help="Local JSON/text/CSV file to infer from."),
    model_name: str = typer.Option("InferredModel", help="Generated model class name."),
    output: Path | None = typer.Option(
        None, help="Optional path to write generated code."
    ),
) -> None:
    """Infer a model from local JSON/text/CSV (explicit content sampling path)."""
    _get_context(ctx)
    text = source.read_text(encoding="utf-8")
    suffix = source.suffix.lower()
    if suffix == ".json":
        result = infer_from_json_text(text, model_name=model_name)
    elif suffix in {".csv", ".tsv"}:
        delimiter = "\t" if suffix == ".tsv" else ","
        lines = text.splitlines()
        headers = [h.strip() for h in lines[0].split(delimiter)]
        rows = [
            [cell.strip() for cell in line.split(delimiter)]
            for line in lines[1:]
            if line.strip()
        ]
        result = infer_from_sheet_headers(headers, rows, model_name=model_name)
    else:
        result = infer_from_text_lines(text.splitlines(), model_name=model_name)

    code = generate_model_code(result)
    binding = result.binding
    console.print("[bold]Binding proposal (local content sample)[/]")
    console.print(f"  storage: {binding.storage}")
    console.print(f"  resource_hint: {binding.resource_hint}")
    console.print("\n[bold]Generated model[/]")
    typer.echo(code)
    if output is not None:
        output.write_text(code, encoding="utf-8")
        console.print(f"[green]Wrote {output}[/]")


@app.command("adopt")
def adopt_cmd(
    ctx: typer.Context,
    model_name: str = typer.Option(..., help="Model name to bind."),
    folder_id: str = typer.Option(..., help="Drive folder ID to adopt."),
    collection_path: str | None = typer.Option(
        None, help="Collection path hint stored in registry."
    ),
    mapping_mode: str = typer.Option("partial", help="Mapping mode for the model."),
    mime: list[str] = typer.Option(None, help="Optional MIME selector(s)."),
    metadata_backend: str = typer.Option(
        "sheets",
        help="Collection metadata backend: sheets or json.",
    ),
    adoption_mode: bool = typer.Option(
        False,
        "--adoption-mode",
        help="Allow a folder tagged for another GDM project.",
    ),
) -> None:
    """Bind a folder as a collection model (dry-run by default)."""
    context = _get_context(ctx)
    planner = AdoptPlanner()
    plan = planner.plan_bind_folder(
        model_name=model_name,
        folder_id=folder_id,
        collection_path=collection_path,
        mapping_mode=mapping_mode,
        mime_patterns=tuple(mime or ()),
    )
    console.print("[bold]Adopt plan[/]")
    console.print(f"  metadata_only: {plan.metadata_only}")
    console.print(f"  destructive: {plan.is_destructive}")
    for write in plan.writes:
        console.print(f"  would: {write}")
    for note in plan.notes:
        console.print(f"[dim]{note}[/]")
    if metadata_backend not in {"sheets", "json"}:
        console.print("[red]--metadata-backend must be 'sheets' or 'json'.[/]")
        raise typer.Exit(code=1)
    binder = (
        context.adopt_binder_factory()
        if context.adopt_binder_factory
        else (
            _build_default_adopt_binder(
                context,
                metadata_backend=metadata_backend,
                adoption_mode=adoption_mode,
            )
            if not context.dry_run
            else None
        )
    )
    result = planner.apply_plan(plan, binder=binder, dry_run=context.dry_run)
    console.print(result.message)
    if result.binding:
        console.print(json.dumps(result.binding, indent=2, sort_keys=True))


@app.command("doctor")
def doctor_cmd(
    ctx: typer.Context,
    model: str | None = typer.Option(None, help="Optional model name to inspect."),
    folder_id: str | None = typer.Option(None, help="Collection folder ID."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON report."),
    warn_at: int = typer.Option(
        1000,
        min=1,
        help="Warn when direct collection membership reaches this count.",
    ),
) -> None:
    """Reconcile collection membership/metadata without reading file bodies."""
    context = _get_context(ctx)
    gdm_dir = _gdm_dir(context.project_dir)
    issues = 0

    if not gdm_dir.exists():
        console.print("[red]Missing .gdm project metadata. Run `gdm init` first.[/]")
        raise typer.Exit(code=1)

    registry = (
        context.schema_registry_factory()
        if context.schema_registry_factory
        else SchemaRegistry(str(_schemas_dir(context.project_dir)))
    )

    drift_report = None
    selected_snapshot = None
    if model:
        applied = registry.load(model, applied=True)
        current = registry.load(model, applied=False)
        selected_snapshot = current or applied
        drift_report = detect_drift(
            model_snapshot=current,
            applied_snapshot=applied,
            live_schema=None,
        )
        for diag in drift_report.diagnostics:
            color = {
                DriftSeverity.ERROR: "red",
                DriftSeverity.WARNING: "yellow",
                DriftSeverity.INFO: "blue",
            }[diag.severity]
            console.print(f"[{color}]{diag.severity.value}[/] {diag.message}")
            if diag.action:
                console.print(f"  action: {diag.action}")
            if diag.severity is DriftSeverity.ERROR:
                issues += 1
    else:
        schema_files = list(_schemas_dir(context.project_dir).glob("*.json"))
        if not schema_files:
            console.print("[yellow]No schema snapshots found.[/]")
        for path in schema_files:
            console.print(f"[dim]Found schema snapshot: {path.name}[/]")

    metadata_store = None
    if context.metadata_store_factory:
        metadata_store = context.metadata_store_factory()
    if model and folder_id:
        folder_file_ids: list[str] = []
        mime_types: list[str] = []
        folder_names: list[str] = []
        client = None
        if context.explorer_factory:
            report = context.explorer_factory().explore(folder_id)
        else:
            client = _build_default_client(context)
            report = client.explore(folder_id, max_resources=max(warn_at + 1, 1001))
        direct_resources = [
            resource
            for resource in report.inventory.resources
            if resource.parent_id == folder_id
        ]
        folder_file_ids = [resource.id for resource in direct_resources]
        mime_types = [resource.mime_type for resource in direct_resources]
        folder_names = [resource.name for resource in direct_resources]

        if metadata_store is None and client is not None:
            metadata_store = _existing_collection_metadata_store(client, model)

        indexed_fields = (
            [meta.name for meta in selected_snapshot.fields if meta.index]
            if selected_snapshot is not None
            else []
        )
        stale_indexes: list[str] = []
        index_store = (
            context.index_store_factory()
            if context.index_store_factory
            else IndexStore(str(_indexes_dir(context.project_dir)))
        )
        loaded_index_fields = [
            field_name
            for field_name in indexed_fields
            if index_store.load(model, field_name) is not None
        ]
        if (
            client is not None
            and loaded_index_fields
            and client.project.registry.get(model) is not None
            and metadata_store is not None
        ):
            from gdm.migration_target import build_production_migration_target

            target = build_production_migration_target(
                client,
                indexes_dir=str(_indexes_dir(context.project_dir)),
            )
            live_fingerprint = target.index_fingerprint(model)
            stale_indexes = [
                field_name
                for field_name in loaded_index_fields
                if (index := index_store.load(model, field_name)) is not None
                and index.source_fingerprint != live_fingerprint
            ]

        collection_report = run_collection_doctor(
            model_name=model,
            folder_id=folder_id,
            folder_file_ids=folder_file_ids,
            mime_types=mime_types,
            folder_names=folder_names,
            metadata_store=metadata_store,
            drift_report=drift_report,
            indexed_fields=indexed_fields,
            stale_indexes=stale_indexes,
            large_collection_threshold=warn_at,
        )
        if json_output:
            typer.echo(json.dumps(doctor_payload(collection_report), indent=2))
        else:
            for issue in collection_report.issues:
                color = "red" if issue.severity == "error" else "yellow"
                if issue.severity == "info":
                    color = "blue"
                console.print(f"[{color}]{issue.kind.value}[/] {issue.message}")
            console.print(
                "[dim]Content capabilities (no body reads): "
                f"{', '.join(collection_report.content_capabilities) or '-'}[/]"
            )
        issues += collection_report.error_count

    index_root = _indexes_dir(context.project_dir)
    if index_root.exists():
        index_count = sum(1 for _ in index_root.rglob("*.json"))
        console.print(f"[dim]Sidecar indexes: {index_count} file(s)[/]")
    else:
        console.print("[yellow]No index directory yet.[/]")

    if issues:
        raise typer.Exit(code=1)
    console.print("[green]Doctor checks completed (metadata-only).[/]")


@migrate_app.command("plan")
def migrate_plan_cmd(
    ctx: typer.Context,
    model: str = typer.Option(..., help="Model name to plan migrations for."),
    resource_id: str = typer.Option(..., help="Bound folder/sheet resource ID."),
    allow_destructive: bool = typer.Option(
        False, help="Include destructive operations."
    ),
) -> None:
    """Build a dry-run migration plan."""
    context = _get_context(ctx)
    del context
    registry = SchemaRegistry(str(_schemas_dir(_get_context(ctx).project_dir)))
    applied = registry.load(model, applied=True)
    current = registry.load(model, applied=False)
    if current is None:
        console.print(f"[red]No current schema snapshot for '{model}'.[/]")
        raise typer.Exit(code=1)

    planner = MigrationPlanner()
    plan = planner.plan_from_snapshots(
        model_name=model,
        current=applied or current,
        target=current,
        resource_id=resource_id,
        dry_run=True,
        allow_destructive=allow_destructive,
    )
    blocked = plan.blocked_reason()
    if blocked:
        console.print(f"[red]{blocked}[/]")
        raise typer.Exit(code=1)

    for op in plan.operations:
        flag = "[red]destructive[/]" if op.destructive else "[green]metadata-only[/]"
        console.print(f"{flag} {op.kind.value}: {op.summary()}")
    console.print(f"[dim]Dry-run plan with {len(plan.operations)} operation(s).[/]")


@migrate_app.command("apply")
def migrate_apply_cmd(
    ctx: typer.Context,
    model: str = typer.Option(..., help="Model name to migrate."),
    resource_id: str = typer.Option(..., help="Bound folder/sheet resource ID."),
    allow_destructive: bool = typer.Option(False, help="Allow destructive operations."),
    checkpoint_file: Path | None = typer.Option(
        None, help="Resume from checkpoint JSON."
    ),
) -> None:
    """Apply a migration plan (requires --apply global flag)."""
    context = _get_context(ctx)
    if context.dry_run:
        console.print(
            "[yellow]Refusing to apply while --dry-run is active. Pass --apply.[/]"
        )
        raise typer.Exit(code=1)

    registry = SchemaRegistry(str(_schemas_dir(context.project_dir)))
    applied = registry.load(model, applied=True)
    current = registry.load(model, applied=False)
    if current is None:
        console.print(f"[red]No current schema snapshot for '{model}'.[/]")
        raise typer.Exit(code=1)

    planner = MigrationPlanner()
    plan = planner.plan_from_snapshots(
        model_name=model,
        current=applied or current,
        target=current,
        resource_id=resource_id,
        dry_run=False,
        allow_destructive=allow_destructive,
    )
    target = (
        context.migration_target_factory()
        if context.migration_target_factory
        else _build_default_migration_target(
            context,
            model_name=model,
            resource_id=resource_id,
        )
    )
    checkpoint_path = checkpoint_file or _default_checkpoint_path(
        context.project_dir, model
    )
    checkpoint = None
    if checkpoint_file and checkpoint_file.is_file():
        checkpoint = load_checkpoint(checkpoint_file.read_text(encoding="utf-8"))
    elif checkpoint_path.is_file():
        checkpoint = load_checkpoint(checkpoint_path.read_text(encoding="utf-8"))

    def on_checkpoint(cp: MigrationCheckpoint) -> None:
        _persist_checkpoint(checkpoint_path, cp, plan)
        save = getattr(target, "save_checkpoint", None)
        if callable(save):
            save(cp)

    engine = MigrationEngine(target)
    report = engine.apply(plan, checkpoint=checkpoint, on_checkpoint=on_checkpoint)

    for result in report.results:
        console.print(f"{result.status}: {result.message}")
    if report.partial_failure:
        console.print("[red]Migration stopped after partial failure.[/]")
        if report.checkpoint is not None:
            _persist_checkpoint(checkpoint_path, report.checkpoint, plan)
        raise typer.Exit(code=1)
    if report.succeeded and current is not None:
        registry.save(current, applied=True)
        if report.checkpoint is not None:
            _persist_checkpoint(checkpoint_path, report.checkpoint, plan)
        console.print("[green]Migration applied; updated applied schema snapshot.[/]")


@migrate_app.command("rollback")
def migrate_rollback_cmd(
    ctx: typer.Context,
    model: str = typer.Option(..., help="Model name to roll back."),
    resource_id: str = typer.Option(..., help="Bound folder/sheet resource ID."),
    allow_destructive: bool = typer.Option(
        False, help="Allow destructive rollback operations."
    ),
    checkpoint_file: Path | None = typer.Option(
        None, help="Apply checkpoint JSON with completed_op_ids."
    ),
) -> None:
    """Roll back a completed migration (dry-run by default; pass --apply to write)."""
    context = _get_context(ctx)
    registry = SchemaRegistry(str(_schemas_dir(context.project_dir)))
    current = registry.load(model, applied=False)
    if current is None:
        console.print(f"[red]No current schema snapshot for '{model}'.[/]")
        raise typer.Exit(code=1)

    checkpoint_path = checkpoint_file or _default_checkpoint_path(
        context.project_dir, model
    )
    if not checkpoint_path.is_file():
        console.print(
            "[red]Missing migration checkpoint. "
            "Apply a migration first or pass --checkpoint-file.[/]"
        )
        raise typer.Exit(code=1)
    checkpoint_raw = checkpoint_path.read_text(encoding="utf-8")
    apply_checkpoint = load_checkpoint(checkpoint_raw)
    if not apply_checkpoint.completed_op_ids:
        console.print(
            "[red]Checkpoint has no completed_op_ids; "
            "nothing was applied to roll back.[/]"
        )
        raise typer.Exit(code=1)

    stored_plan = load_checkpoint_plan(checkpoint_raw)
    if stored_plan is not None:
        plan = MigrationPlan(
            model_name=stored_plan.model_name,
            operations=stored_plan.operations,
            from_snapshot=stored_plan.from_snapshot,
            to_snapshot=stored_plan.to_snapshot,
            dry_run=context.dry_run,
            allow_destructive=allow_destructive,
        )
    else:
        applied = registry.load(model, applied=True)
        planner = MigrationPlanner()
        plan = planner.plan_from_snapshots(
            model_name=model,
            current=applied or current,
            target=current,
            resource_id=resource_id,
            dry_run=context.dry_run,
            allow_destructive=allow_destructive,
        )

    target = (
        context.migration_target_factory()
        if context.migration_target_factory
        else _build_default_migration_target(
            context,
            model_name=model,
            resource_id=resource_id,
        )
    )

    def on_checkpoint(cp: MigrationCheckpoint) -> None:
        _persist_checkpoint(checkpoint_path, cp, plan)
        save = getattr(target, "save_checkpoint", None)
        if callable(save):
            save(cp)

    engine = MigrationEngine(target)
    report = engine.rollback(
        plan,
        apply_checkpoint,
        checkpoint=apply_checkpoint,
        allow_destructive=allow_destructive,
        on_checkpoint=on_checkpoint if not context.dry_run else None,
    )

    for result in report.results:
        console.print(f"{result.status}: {result.message}")
    if report.results and report.results[0].status == "blocked":
        raise typer.Exit(code=1)
    if context.dry_run:
        console.print(
            f"[dim]Dry-run rollback for {len(apply_checkpoint.completed_op_ids)} "
            "completed operation(s); no writes performed.[/]"
        )
        return
    if report.partial_failure:
        console.print("[red]Rollback stopped after partial failure.[/]")
        if report.checkpoint is not None:
            _persist_checkpoint(checkpoint_path, report.checkpoint, plan)
        raise typer.Exit(code=1)
    if report.rollback_succeeded and plan.from_snapshot is not None:
        registry.save(plan.from_snapshot, applied=True)
        if checkpoint_path.is_file():
            checkpoint_path.unlink()
        console.print("[green]Rollback completed; restored applied schema snapshot.[/]")


@index_app.command("rebuild")
def index_rebuild_cmd(
    ctx: typer.Context,
    model: str = typer.Option(..., help="Model name."),
    field: str = typer.Option(..., help="Indexed field name."),
    resource_id: str = typer.Option(
        ..., help="Collection folder or sheet resource ID."
    ),
    unique: bool = typer.Option(False, help="Treat index as unique."),
    policy: StalePolicy = typer.Option(StalePolicy.REBUILD, help="Stale index policy."),
    content_indexed: bool = typer.Option(
        False,
        help="Set only when an explicit content extractor produced values.",
    ),
) -> None:
    """Rebuild a sidecar index keyed by Drive file ID (dry-run by default)."""
    context = _get_context(ctx)
    store = (
        context.index_store_factory()
        if context.index_store_factory
        else IndexStore(str(_indexes_dir(context.project_dir)))
    )
    fingerprints = compute_collection_fingerprints(
        folder_id=resource_id,
        content_indexed=content_indexed,
    )
    fingerprint = fingerprints.combined()
    maintainer = IndexMaintainer(store)

    label = "metadata-only" if not content_indexed else "content-indexed"
    if context.dry_run:
        console.print(
            f"[dim]Dry-run ({label}): would rebuild index for {model}.{field} "
            f"with fingerprint {fingerprint[:12]}… (policy={policy.value})[/]"
        )
        return

    if context.index_store_factory:
        maintainer.rebuild_from_records(
            model_name=model,
            field_name=field,
            unique=unique,
            records=(),
            source_fingerprint=fingerprint,
            fingerprints=fingerprints,
            content_indexed=content_indexed,
        )
        indexed_count = 0
    else:
        if content_indexed:
            console.print(
                "[red]Content-indexed rebuilds require an explicit programmatic "
                "content extractor; the CLI only reads metadata.[/]"
            )
            raise typer.Exit(code=1)
        target = _build_default_migration_target(
            context,
            model_name=model,
            resource_id=resource_id,
        )
        indexed_count = target.rebuild_index(model, field, unique=unique)
    console.print(f"[green]Rebuilt index at {store.path_for(model, field)}[/]")
    console.print(f"[dim]Indexed {indexed_count} record(s).[/]")


@auth_app.command("login")
def auth_login_cmd(
    ctx: typer.Context,
    scopes: str = typer.Option(
        "drive,spreadsheets",
        help="Comma-separated OAuth scopes (never logged).",
    ),
    client_secrets: Path | None = typer.Option(
        None,
        help="Path to OAuth client secrets JSON (or set GDM_CLIENT_SECRETS).",
    ),
) -> None:
    """Start interactive OAuth login (requires google-auth-oauthlib extra)."""
    context = _get_context(ctx)
    if context.auth_login_factory:
        code = context.auth_login_factory()
        raise typer.Exit(code=code)

    from gdm.auth import login_interactive, resolve_client_secrets_path

    requested = [s.strip() for s in scopes.split(",") if s.strip()]
    secrets = client_secrets or resolve_client_secrets_path()
    if secrets is None:
        console.print(
            "[red]OAuth client secrets required. Pass --client-secrets or set "
            "GDM_CLIENT_SECRETS.[/]"
        )
        raise typer.Exit(code=1)
    console.print(
        "[dim]Starting OAuth login; tokens are stored outside the project.[/]"
    )
    login_interactive(scopes=requested, client_secrets=secrets)
    console.print("[green]Login completed.[/]")


def _build_default_explorer(max_resources: int) -> Explorer:
    from gdm.auth import build_explorer_service

    service = build_explorer_service(use_adc=True)
    return Explorer(service, max_resources=max_resources)


def run() -> None:
    """Console entry point."""
    app()


if __name__ == "__main__":
    run()
