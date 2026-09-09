# Model inference

**`gdm infer`** suggests Pydantic field types and `Field()` metadata—**read-only**, never
mutating remote sources.

## Collection inference (metadata only)

```console
gdm infer collection --folder-id YOUR_FOLDER_ID --model-name CampaignAsset
gdm infer collection --root-id ROOT_ID --folder-id FOLDER_ID --output models/asset.py
```

Uses Drive file metadata and optional metadata-table headers. **No Docs/Slides/media downloads.**

## Local content sampling (explicit)

When you intentionally sample file bodies offline:

```console
gdm infer file sample.json --model-name ReleaseNote
gdm infer file rows.csv --model-name Row --output models/row.py
```

Supported local suffixes: `.json`, `.csv`, `.tsv`, plain text. Plain-text
sampling infers `codec="text"` with one complete UTF-8 body field—not line
records.

## Output

Inference produces:

- suggested Python types
- `drive=` / `metadata=` / sheet `source=` mappings
- primary-key candidates
- binding proposal JSON with `infer collection --json`

Copy the suggestion into your model module, adjust it, then use
`client.bind_collection()` for folders or `client.bind()` for worksheets.

Generated models use the public `Field()` targets directly: `source=` for
worksheet headers, `drive=` for Drive metadata, and `metadata=` for collection
metadata columns. Review inferred types and MIME selectors before binding.

## Strict vs partial

Inference assumes **`partial`** mapping unless you pass
`--mapping-mode strict` to `gdm adopt`. Unknown columns in strict mode require
migration or cleanup.

## Related

- [Define a model](../tutorial/model.md)
- [Adopt Drive folders](../adoption/drive-collections.md)
- [Schema drift](../migrations/drift.md)
