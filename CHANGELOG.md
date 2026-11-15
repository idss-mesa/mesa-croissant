# Changelog

All notable changes to mesa-croissant. `CORE_VERSION` in `src/mesa_croissant/core.py` is the
package version; the portal vendors `core.py` byte-identical and pins the same version.

## 1.3.1 (2026-11-15)

Fixes from a live test of the five MESA-MCP tools on the public NEON DuckLake
(`/iplant/home/shared/mesa/lakehouse/neon/neon-ducklake`), checked against mlcroissant
1.1.1 and real NEON Parquet. Convention version 1 is unchanged: AVUs written by 1.3.0 still
parse and export (tests).

### Type map, globs and record loading (pass 1)

- **GEOMETRY / WKB_BLOB** map to `[sc:GeoShape, sc:Text]`: mlcroissant 1.1.1 refuses
  `sc:GeoShape` alone and reads the WKB bytes as text. The spatial types POINT_2D, BOX_2D,
  LINESTRING_2D and POLYGON_2D map to their Parquet STRUCT layout as subFields.
  `duckdb_type_to_croissant` returns `dataTypes` (what writers store) next to `dataType` (the
  type mlcroissant reads).
- `validate` warns, without an error, when no `dataType` of a field is one mlcroissant can read
  (and the field has no subFields and no field source).
- **MAP(K, V)** becomes an `isArray` field with subFields `key` / `value` read with `$[0]` /
  `$[1]`; as `sc:Text` it failed the whole record set.
- **FileSet globs.** `suggest_distribution` writes `<folder>/*.parquet` and
  `<folder>/**/*.parquet`: mlcroissant's `fnmatch` needs a sub-folder for `**/`, so the old
  glob found no file of an unpartitioned DuckLake table.
- `schema_parity.json` gains a MAP and a GEOMETRY column; Parquet record-loading compat tests
  (CI installs `mlcroissant[parquet]`).

### Input checks, limits and documentation (pass 2)

- **`mesa_croissant_from_schema`**
  - Field ids are unique: a name with whitespace gets the default id with whitespace runs
    replaced by `_`, plus the first free `-2`, `-3`, ... when that id is another field's
    default or taken (`a b` next to `a_b` gave both `t/a_b`). Deterministic; real names keep
    their default ids.
  - `column` keeps the exact column name (it was trimmed, so `'  padded  '` read a column
    `padded`); the field `name` is trimmed. Such a column gets a warning, since MESA writers
    store values trimmed.
  - Warnings name the emitted field's AVU path; a skipped (blank or duplicate) column is
    named `columns[i]` (the input position used to name a different, emitted field).
  - Values over the AVU limits (2,700 / 250 characters, NUL) are warnings on their attribute.
  - `MAX_COLUMNS` is derived from the 5,000-AVU apply limit: (5,000 - 3) // 4 = 1,249 (was
    2,000, about 8,000 AVUs), and a result over 5,000 AVUs is refused.
  - `STRUCT()` (no members) is typed `sc:Text` with a warning (it gave a field with no
    dataType and no subFields).
- **`mesa_croissant_apply`**
  - `avus` must use canonical keys: index 0, leading zeros (`01`, `007`), signs (`-1`), empty
    segments and gaps in a list are import errors naming the attributes, and so are two values
    for one single-valued path or index (`croissant.name` twice, `croissant.license` next to
    `croissant.license.1`), which was only a warning. Unknown keys stay accepted with a
    warning. Imported documents go through the same checks and never trip them.
  - Trimming is documented and reported: new attributes, values and units are written trimmed
    (mesa-mcp's iRODS helper strips every triple), with a warning per trimmed attribute.
  - More than 5,000 AVUs is refused with `invalid_argument` before the diff, in a dry run too
    (a refused dry run returned the whole diff and validation, 3.7 MB in the test).
- **Deep JSON.** `core.MAX_JSON_DEPTH` (100): `validate` and `from_jsonld` report a deeper
  document as one error instead of raising RecursionError, and `mesa_croissant_validate` /
  `mesa_croissant_apply` refuse it with `invalid_argument`. Inline `data` / `examples` JSON
  deeper than 64 levels is emitted as text with a warning.
- **Template.** `required` means required where the key lives (in the dataset, or in each
  given entry), so `publisher.N.name` is now required like `creator.N.name`. A field's
  `dataType` is listed in both forms: the bare `...field.M.dataType` (what writers write) and
  `...field.M.dataType.K`.
- **Docs.** Convention rules 3 (whitespace: readers never trim, writers trim), 5 and 6 (what
  apply refuses), field `id` and `column`; DESIGN and README describe the new checks and limits
  and list the expected behaviours the live test confirmed (dav-anon redirects, validator
  strictness, argument coercion, ...).

## 1.3.0 (2026-11-08)

- `croissant.*` AVU convention version 1, the pure-stdlib core (codec, generator, crosswalk,
  importer, validator, encoding-format and DuckDB type maps) and the five `mesa_croissant_*`
  MESA-MCP tools.
