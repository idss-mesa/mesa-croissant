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
    their default ids (see pass 3 for the cases pass 2 missed).
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

### Verification fixes (pass 3)

- **`mesa_croissant_apply` merge checks gaps on the result.** Pass 2 ran the gap check on the
  new set alone, so merge refused every partial update starting at index 2 or higher
  (`creator.2` alone, a new `keyword.3`, `...field.3`, record set 2 from
  `from_schema(record_set_index=2)`), which 1.3.0 accepted. Merge now checks the new set plus
  the entries that stay: a new entry is refused only when the write leaves a gap before it
  (a new index after a missing one, or an entry the merge removes). Entries already stored
  after an old gap can still be edited; stored keys are read as readers read them (`01` = 1).
  `replace` still checks the new set alone. All other key checks are unchanged.
- **Refused dry runs are bounded.** A value over 2,700 characters (or a unit over 250) in the
  diff is clipped to its first 200 characters plus its length: a dry run with a
  1,200,000-character description returned 2.4 MB, now about 5 kB.
- **`mesa_croissant_from_schema` ids.** Real names now keep their default ids in the cases
  pass 2 missed: a real member of a renamed STRUCT (`s t` next to `s_t`: the member `x_y`
  keeps `t/s_t-2/x_y`, `x y` gets `t/s_t-2/x_y-2`), a trimmed name next to the exact one
  (`a ` next to `a`: `a` keeps `t/a`) and a column name with `/` equal to a member path
  (`s_t/a` keeps `t/s_t/a`).

### Validator, keywords and encoding formats (pass 4)

The deferred findings of the live test. Convention version 1 is unchanged, and `croissant.*`
AVUs written by 1.3.0 parse and export as before (test); the changes below are in what the
crosswalk and the generator derive and in validator warnings.

- **`@graph` documents.** `validate` reads the Dataset node of a top-level `@graph` exactly as
  `from_jsonld` does (one shared function): the live test's wrapped document had 8 errors in
  `validate` and imported cleanly. More than one Dataset node is now an error at `@graph` in
  both (`from_jsonld` silently took the first).
- **`encodingFormat` on every FileObject and FileSet.** Unknown extensions fall back to
  `application/octet-stream` (a file's own FileObject and distribution entries without
  `croissant.*.encodingFormat`); `pyproject.toml` exported without it, which mlcroissant
  refuses. The extension table gains text, code and configuration types: `toml`
  `application/toml`, `sql` `application/sql`, `js` `text/javascript`, `py` `text/x-python`,
  `ipynb` `application/x-ipynb+json`, `r` `text/x-rsrc`, `rmd` / `qmd` `text/markdown`, `cff`
  `application/yaml`, `lock` / `log` / `ini` / `cfg` / `conf` `text/plain`, and `jl`, `sh`,
  `css`, `c` / `h` / `cpp`, `java`, `rst`, `tex`, `bib`, `docx`, `pptx`, `rdf`, `owl`, `nt`,
  `nq`, `gml`, `kmz`, `csl` (IANA types where registered, else Python's `mimetypes` /
  shared-mime-info). `encoding_format_for` still returns `None` for an unknown extension;
  a file's import (`croissant.file.*`) leaves out an `encodingFormat` that is only the
  derived type (writers' handling of distribution entries: pass 5).
- **mlcroissant warnings in `validate`** (warnings, not errors; the README and DESIGN list
  them): `sc:DefinedTerm` keywords (with the hint to use `keyword_style` `text`), an
  `@context` that is not an object (mlcroissant refuses a string or list), and documents using
  `http://schema.org/`.
- **`mesa_croissant_validate` takes `keyword_style`** (values, default and description as in
  export) for a `path`; it always validated the DefinedTerm document, so a folder mlcroissant
  rejects was reported valid without a hint. The hint names `keyword_style`.
- **Ontology keyword names.** An ontology AVU's `DefinedTerm` is named by the term's label when
  a `datacite.subject.N` names the term (its `classificationCode` or `valueURI` is the CURIE
  or its OBO PURL, or its value ends with `(<CURIE>)`), and takes that subject's place, so
  the term is no longer listed twice. Otherwise the AVU value stays the name (an editor that
  picks a sub-term keeps the parent field's key, so the attribute key is not a label). On
  sites/KONZ, `NLCD 2006 deciduousForest 3.27 km2 (9.5%)` becomes `area of deciduous forest`.
  Further AVUs of a term a subject names are not listed again (unnamed terms: once per value
  and term, see pass 5). The crosswalk's suggested `croissant.keyword` AVUs carry the same
  names.
- **Text keywords are de-duplicated.** With `keyword_style` `text`, a name already listed
  (compared case-insensitively) is left out, keeping the first spelling and the order (KONZ
  listed `Tallgrass prairie` twice).
- **Import reports what a keyword AVU cannot hold.** A `DefinedTerm`'s `inDefinedTermSet`,
  and its `url` next to a `termCode`, are dropped with a warning (they were dropped silently,
  against the `from_jsonld` docstring). No key was added: a sub-key of the repeatable
  `croissant.keyword.N` would stay at its old index when a 1.3.0 reader renumbers the keywords.

### Cross-version saves, message caps and smaller fixes (pass 5)

Follow-ups of the verification of passes 1-4. Convention version 1 is unchanged and
`croissant.*` AVUs written by 1.3.0 parse and export as before (test).

- **1.3.0 exports pin no keywords.** Pass 4 changed the keyword list the crosswalk derives
  (KONZ: 14 keywords in 1.3.0, 10 now), so `drop_generated` no longer recognised a 1.3.0
  export of an unchanged folder: apply (merge or replace, either keyword style) stored
  `croissant.keyword.1-14`, freezing the old duplicated list. Keywords now also count as
  generated when they give the 1.3.0 list, which the new `core.legacy_keyword_avus(avus)`
  rebuilds (checked equal to the 1.3.0 crosswalk on KONZ). The other direction cannot be
  fixed here: a 1.3.1 export saved through a 1.3.0 reader (the live portal, mesa-mcp with
  1.3.0) stores 10 keyword AVUs. Deploy the portal and mesa-mcp with the same core
  (convention section 7, README).
- **Derived `encodingFormat`s are not added.** The generator writes an `encodingFormat` on
  every entry, so applying an unedited export of a folder written by 1.3.0 added
  `croissant.distribution.N.encodingFormat` for every entry stored without one
  (`application/octet-stream`, or a newly mapped type such as `application/toml`).
  `drop_generated` now leaves out a value equal to the type the generator derives for entry N
  when the item stores entry N without one (`core.generated_encoding_formats(new, current)`,
  one warning at `croissant.distribution`). A new entry, or one stored with an
  `encodingFormat`, keeps the document's value, so applying a document to a new folder
  stores what it did, and stored values written by `suggest_distribution` stay unchanged.
  A stored `croissant.file.encodingFormat` equal to the derived type is still removed by
  `replace` (the document is the same); documented in convention section 5.1.
- **Message caps in the MCP tools.** `mesa_croissant_validate`, `mesa_croissant_export` and
  `mesa_croissant_apply` return at most 200 validation errors and warnings, apply warnings
  and import errors (also in a `croissant_invalid` error's details), with
  `errors_truncated` / `warnings_truncated` / `import_errors_truncated` counting the rest
  when something was left out, as the portal's convert does. Validating 5,000 bare fields
  returned 2.4 MB (now 48 KB through the live server); an accepted 5,000-AVU dry run 3.3 MB
  (now 0.96 MB, the diff itself, which is not cut). `valid` and `counts` count everything.
- **Keyword labels.** A subject that names a term by `classificationCode` or `valueURI` and
  also ends with `(<CURIE>)` gave the name with the suffix; the label now drops a final
  `(<CURIE>)` or `(<PURL>)` of the term however the subject names it. An `https://` OBO PURL
  in `valueURI` names the term too. Convention section 7 now says that only a term a
  subject names is listed once; unnamed ontology AVUs stay one `DefinedTerm` per value and
  term, as in 1.3.0.
- **`http://schema.org/` warning.** `validate` warned whenever the context's `sc` / `schema`
  prefix was `http://schema.org/`, even unused, although mlcroissant 1.1.1 loads such a
  document. It now expands the terms the document uses (the Dataset `@type`, other `@type`s,
  keys, `dataType` values) with its `@context` and warns only for those: about the Dataset
  type ("mlcroissant does not find the Dataset", which mlcroissant confirms) or naming the
  first other term (mlcroissant loads those documents but does not read the terms).
- **Vendored copy check.** The module docstring and the README now say that the portal pins
  the copied file's SHA-256 next to `CORE_VERSION`: the pass-3 copy on the portal branch
  declared 1.3.1 too, so a version-only check could not catch it.
- Tests: core (labels, unnamed terms, `legacy_keyword_avus`, `generated_encoding_formats`,
  1.3.0 AVUs, `http://` terms), plugin (caps in validate / export / apply, 1.3.0 keyword
  exports in both styles and modes, derived and edited `encodingFormat`s), mlcroissant
  compat (an unused `http://` prefix loads without a warning; the Dataset type warning
  matches mlcroissant's refusal).

## 1.3.0 (2026-11-08)

- `croissant.*` AVU convention version 1, the pure-stdlib core (codec, generator, crosswalk,
  importer, validator, encoding-format and DuckDB type maps) and the five `mesa_croissant_*`
  MESA-MCP tools.
