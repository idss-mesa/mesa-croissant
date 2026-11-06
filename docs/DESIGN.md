# MESA Croissant: design

Status: version 1, 2026-11-06. Owner: MESA (tswetnam@unm.edu).

## Goal

Make MLCommons Croissant (Core 1.1, RAI 1.0, GeoCroissant 1.0) a first-class metadata
standard in the MESA ecosystem:

1. Folders and files in the CyVerse Data Store carry Croissant metadata as AVUs, in the
   MESA-MCP / MESA-DuckLake flat `namespace.key` style ([AVU_CONVENTION.md](AVU_CONVENTION.md)).
2. The MESA Portal shows and edits it: a **Croissant** tab on the metadata page
   (`/data/metadata/page/`) with a structure map, forms for dataset / RAI / geo / distribution /
   record sets, a live JSON-LD preview, validation, JSON-LD import and download.
3. Agents annotate the same AVUs through MESA-MCP tools (`mesa_croissant_*`), so a dataset
   annotated by an agent and one annotated in the portal are identical.
4. DuckLake projects get Croissant record sets from their table schemas.

## Components

| Component | Where | What |
|---|---|---|
| `mesa_croissant.core` | this repo, `src/mesa_croissant/core.py` | pure-stdlib codec: AVUs ⇄ nested model ⇄ Croissant JSON-LD, MESA crosswalk, validator, encoding-format and DuckDB type maps |
| `mesa_croissant.mcp_tools` | this repo | MESA-MCP plugin (entry point group `mesa_mcp.tools`, surface `croissant`) |
| Portal service | `mesa-portal/portal/services/croissant_core.py` | **vendored copy** of `core.py` (byte-identical; a test compares the declared version) |
| Portal glue | `mesa-portal/portal/services/croissant_service.py`, `portal/views/croissant_views.py`, routes in `portal/urls/viewer_urls.py` | reads AVUs / stat / listing through `DataService`, adds portal URLs |
| Portal UI | `portal/templates/viewers/metadata.html` (Croissant tab), `data_browser.html` (folder header chip), `viewers/duckdb.html` (record-set export) | |
| Golden fixtures | `tests/fixtures/*.json` in this repo, copied to the portal's `tests/fixtures/croissant/` | AVU lists with their expected JSON-LD, and upstream documents for import |

`core.py` has no third-party imports and no I/O so the same file runs in the portal
(Python 3.11, Django) and in MESA-MCP (Python ≥ 3.11). It declares
`CONVENTION_VERSION = 1` and `CORE_VERSION = "<semver>"`; the portal copy must carry the same
`CORE_VERSION`. Server-side validation is this module's own structural validator; `mlcroissant`
is **not** a runtime dependency (it pulls pandas, scipy, rdflib, networkx). The README
documents `mlcroissant validate --jsonld <file>` for a full check.

Generated documents use `https://schema.org/` (`@vocab` and `sc:`), as every upstream example
does; mlcroissant 1.1.1 does not find the Dataset of an `http://schema.org/` document. Import
accepts both. Two mlcroissant 1.1.1 behaviours shape the output
(`tests/test_mlcroissant_compat.py` checks both):

- it rejects `keywords` given as `sc:DefinedTerm` objects, which Croissant 1.1 allows and the
  generator writes for ontology keywords by default. `item["keyword_style"] = "text"` (the
  portal's `&keywords=text`, the MCP export's `keyword_style: text`) writes plain names instead;
- it requires `md5` or `sha256` on every FileObject unless `isLiveDataset` is true. A file's
  own FileObject takes the MD5 of the Data Store stat (`item["md5"]`); distribution entries need
  `croissant.distribution.N.md5` / `sha256`. The validator warns when both are missing.

## `core.py` public API

```python
CONVENTION_VERSION: int            # 1
CORE_VERSION: str                  # "1.3.0"
PREFIX = "croissant."
CONFORMS_CORE, CONFORMS_RAI, CONFORMS_GEO   # the three URIs
CONTEXT: dict                      # Croissant 1.1 @context (https://schema.org/) + rai, geocr, prov, dct
VALUE_MAX, UNIT_MAX                # 2700, 250 (AVU limits)
UNKNOWN_KEY = "__unknown"          # an entity's own unknown sub-keys in the model
DUP_KEY = "__dup"                  # a list leaf's extra values at the same index

def is_croissant_attr(attr: str) -> bool
def parse_avus(avus) -> dict
    # avus: iterable of {"attribute"|"attr", "value", "unit"} (or tuples); only croissant.* are read.
    # returns {"model": nested dict/list tree, "unknown": [avu...], "warnings": [...]}
    # a leaf is {"value": str, "unit": str}; lists are ordered by index, gaps closed.
    # unknown keys and index-0 entries go to "unknown", except below an indexed entity
    # (creator.2.orcidVerified): there they stay with the entity as
    # entity[UNKNOWN_KEY] = [{"suffix", "value", "unit"}]. A duplicate single-valued path shows
    # the first value and keeps the others the same way, with a warning. A duplicate list index
    # shows the first value and keeps the others on that leaf, leaf[DUP_KEY] = [{"value", "unit"}]
    # (convention rule 6): they are written under the leaf's attribute and go with it.
def model_to_avus(model) -> list[dict]
    # inverse of parse_avus; [{"attribute","value","unit"}], 1-based, no gaps; each entity's
    # UNKNOWN_KEY entries are written under its new index (an entity holding only those keeps one);
    # a list leaf's DUP_KEY values follow it under the same attribute
def crosswalk(avus) -> dict
    # section 7 defaults: {"<croissant property>": {"value": ..., "source": "datacite.title.1.value",
    #                      "avus": [croissant.* AVUs that would copy it]}}
def to_jsonld(avus, *, item: dict) -> dict
    # item = {"path", "name", "is_folder", "url" (landing page), "content_base" (URL prefix for
    #         relative contentUrl), "size", "md5", "doi", "public" (dav-anon base),
    #         "keyword_style" ("defined_term" | "text")}  -- all optional except path/name
    # returns {"document": <JSON-LD dict>, "sources": {"name": "croissant"|"datacite.title.1.value"|
    #          "item.name"|"item.doi"|"item.url"|"item"|"default"|"auto"},
    #          "validation": validate(document) + the AVU warnings}
    # UNKNOWN_KEY entries are never emitted; an entity holding only unknown keys is left out
def from_jsonld(doc, *, content_base=None, item=None) -> dict
    # tolerant import of Croissant 0.8/1.0/1.1 (+RAI, +geo); returns
    # {"avus": [...], "warnings": [...], "errors": [...]}; values over the limits are reported, not cut
    # content_base: contentUrls below it come back as decoded relative paths (default: item's folder)
    # item: the target (to_jsonld's shape); for a file, its FileObject becomes croissant.file.*
def validate(doc) -> dict
    # {"valid": bool, "errors": [{"path": "recordSet[0].field[1].source", "message": ...}],
    #  "warnings": [...], "conformsTo": [...], "summary": {"distribution": n, "recordSet": n, "field": n}}
def encoding_format_for(name: str) -> str | None   # by extension
def duckdb_type_to_croissant(type_name: str) -> dict
    # {"dataType": "cr:Int32", "isArray": False, "arrayShape": None}; a STRUCT gives dataType None
    # and "subFields": [{"name", "dataType", "isArray", "arrayShape"[, "subFields"]}]
def suggest_distribution(entries, *, lake=None, tables=None) -> list[dict]
    # entries: [{"name", "path" (relative to root), "is_folder", "size", "md5"}]; lake: registry
    # entry or None; tables: the lake's tables ("schema.table" | "table" | {"schema", "table"})
    # returns model-shaped distribution entries (section 5 and 8)
def key_catalog(level="all") -> list[dict]
    # level: dataset|rai|geo|file|distribution|recordset|all;
    # rows {"attribute", "cardinality": "one"|"many", "required", "level", "unit", "description"}
```

The package root (`mesa_croissant`) re-exports the functions and constants above except
`VALUE_MAX`, `UNIT_MAX` and `UNKNOWN_KEY`; import those from `mesa_croissant.core`.

## Portal endpoints (all JSON, `Cache-Control: no-store`)

Added to `portal/urls/viewer_urls.py` (per-worker module, lazy `_page`/`_api` wrappers) under
`data/`, implemented in `portal/views/croissant_views.py`. All need a signed-in user (same as
`GET /data/metadata/`: JSON 401 `please log in`), use `clean_metadata_path`, and never fetch a
URL taken from user input or AVUs.

| Method, route | Body / query | Answer |
|---|---|---|
| `GET data/croissant/` | `?path=&type=folder\|file` (`&keywords=text` → plain-text keywords; `&download=1` → `application/ld+json` attachment `<name>.croissant.json`) | `{success, document, sources, validation, croissant_avu_count, can_edit, permission, item_type}` |
| `POST data/croissant/preview/` | `{path, type, avus, keyword_style?}` (the full AVU list the editor would save) | `{success, document, sources, validation}`; no write; limits: ≤5,000 AVUs, body ≤1 MB |
| `POST data/croissant/convert/` | `{jsonld, path?, type?, avus?}` (jsonld: an object or a string, body ≤1 MB; `avus`: the editor's list, ≤5,000, whose non-Croissant AVUs feed the crosswalk) | `{success, avus, avu_count, warnings, errors, validation}` (each list ≤200, `<name>_truncated` counts the rest); no write. With `path`: contentUrls below the item's folder come back as relative paths, a file's own FileObject becomes `croissant.file.*` (`from_jsonld` item mode), and generated values are left out (`drop_generated`: the default url or DOI URL, the DOI identifier, crosswalk-equal properties). More AVUs than one save carries: `avus: []` and the document is not validated |
| `GET data/croissant/suggest/` | `?path=` (folder) | `{success, distribution: [...model entries], lake: {alias, root} \| null, truncated}`; from one directory listing (≤500 entries, recursing at most into a `data/` child), plus section-8 lake suggestions |

Errors: `{success: false, error, code}` with the metadata views' codes:

- `INVALID` (400)
- `SESSION_EXPIRED` (401, an expired SSO token)
- `FORBIDDEN` (403)
- `NOT_FOUND` (404)
- `TOO_LARGE` (413: more than 1 MB or more than 5,000 AVUs)
- `RATE_LIMITED` (429 `{error, code}` with `Retry-After`: preview, convert and suggest)
- `UNAVAILABLE` (503)
- `SERVER_ERROR` (500)

A caller who is not signed in gets 401 `{error, code: 'AUTH_REQUIRED'}`, with no `success` key.
Saving still goes through the existing
`POST /data/metadata/set/` (full replacement list), so permission checks, unit-variant checks
and the folder-context cache invalidation are unchanged.

## Portal UI

### Metadata page: Croissant tab

`metadata.html` gets a tab button `data-tab="croissant"` placed **after EML and before Local
Contexts** (the ontology tests slice the template from the ontology panel to `<!-- iRODS tab -->`).
`?tab=croissant` opens it directly.

Sub-views (segmented control inside the panel):

1. **Overview**: the structure map. A dataset card (name, license, creators, version, DOI,
   conformance badges Core 1.1 / RAI / Geo, completeness meter of required properties), then
   three columns Distribution → Record sets → Fields, with SVG connectors from each field to its
   source and from a FileObject/FileSet to its `containedIn`. Validation errors and warnings are
   listed below with links to the sub-view that fixes them. Includes a "use this dataset"
   snippet (`mlcroissant.Dataset(jsonld=...)`).
2. **Dataset**: core properties; repeatable creator / publisher / license / keyword / sameAs /
   inLanguage groups. Missing properties show the crosswalk value as placeholder text with its
   source ("from DataCite") and a "Copy into Croissant" button.
3. **Responsible AI**: the RAI 1.0 properties (textareas; "many" properties as repeatable lists).
4. **Geospatial**: spatial coverage (place, box with S/W/N/E inputs and a check, point), CRS,
   resolutions with units, temporal coverage, sampling strategy, spatial bias, bands, record
   endpoint.
5. **Distribution**: FileObject / FileSet cards; "Suggest from folder" calls
   `GET data/croissant/suggest/` and offers each suggestion for adding.
6. **Record sets**: record-set cards with nested field rows (name, dataType select + free text,
   source select from distribution ids, column / jsonPath / fileProperty, references, isArray).
   "Import from DuckDB viewer" appears when the DuckDB viewer left a hand-off for this path
   (section below).
7. **JSON-LD**: the generated document (server preview, debounced), copy and download buttons,
   and "Import JSON-LD" (paste or local file → `POST data/croissant/convert/` → summary of the
   AVUs that would change → applies to the editor state; nothing is written until Save).

Editor state is the `parse_avus` tree of the page's `allAvus` (a JS port of the dotted-path
codec, kept in a template-tag-free section); unknown `croissant.*` AVUs and extra duplicate
values are kept (with their entity, as in core) and saved back.
Save uses `collectAvus()` → a dedicated `collectCroissantAvus()` branch: every non-`croissant.*`
AVU of `allAvus` unchanged plus the editor's `croissant.*` AVUs. Read-only users get the
Overview and JSON-LD views and read-only forms. All user data is rendered with `escHtml` /
`textContent`; events are delegated listeners (no user data in inline handlers). `croissant` is
added to `RESERVED_OLS_PREFIXES`.

### Data browser folder header

`folder_context` adds a relation `{kind: 'croissant', label: <croissant.name or "Croissant">,
href: <metadata page link>&tab=croissant, conforms: ['core'(, 'rai')(, 'geo')], counts:
{distribution, recordSet, field}}` when the folder's own AVUs include any `croissant.*`
attribute. `heroRelationChip` renders it as a chip (`db-chip--croissant`, icon
`fa-bread-slice`, prefix "Croissant"); older page JS ignores unknown kinds.

### DuckDB viewer hand-off

In lake mode and file mode (Parquet / CSV), a **Croissant** button builds model-shaped record
sets (and, in lake mode, the FileSet suggestions) from the viewer's own introspection
(`information_schema.columns`, `DESCRIBE`) with the section-9 type map, and offers "Copy JSON"
and "Open in Croissant editor". The latter stores
`{v: 1, path: <dataset root>, created: <ms>, recordSets, distribution}` in
`localStorage['mesa.croissant.handoff']` (overwritten each time, ignored after 1 hour) and opens
`/data/metadata/page/?path=<root>&type=folder&tab=croissant`. The metadata page offers the import
only when `handoff.path` equals its own path.

## MESA-MCP tools (`mesa_croissant.mcp_tools`)

Registered through the `mesa_mcp.tools` entry point with `meta={"io.mesa/surface": "croissant"}`.

| Tool | Input | Effect |
|---|---|---|
| `mesa_croissant_template` | `{level: dataset\|rai\|geo\|file\|distribution\|recordset\|all}` | `key_catalog` (attribute, cardinality, required, unit, description) plus convention/core versions and notes; no I/O |
| `mesa_croissant_export` | `{path, url?, keyword_style: defined_term\|text}` | reads the item's AVUs and stat (file size, MD5 checksum) → `to_jsonld` → `{path, path_target_type, document, sources, validation, croissant_avu_count}` |
| `mesa_croissant_validate` | `{document}` or `{path}` | `validate` report plus `{checked, path, hint}` (the hint names `mlcroissant validate`) |
| `mesa_croissant_apply` | `{path, document? \| avus?, mode: merge\|replace, drop_unknown=false, dry_run=true, content_base?}` | the new set (below) → AVU diff against the item's `croissant.*` AVUs → `{diff: {add, remove, unchanged}, counts, written, errors, kept, warnings, import_errors, validation, partial_failure?}` (`validation`: the document the item would generate); unless `dry_run`, writes (adds first, removes by the exact stored triple) and mirrors one snapshot to the DuckLake AVU history (`record_avu_changes`, source `mesa-mcp:mesa_croissant_apply`) |
| `mesa_croissant_from_schema` | `{columns: [{name, type}], record_set_id, source_id, record_set_index=1, record_set_name?, description?}` | record-set AVUs from a DuckDB/Arrow schema (section 9); STRUCT members get `transformJsonPath` = the member name (`$['…']` when it is not a plain identifier, as the DuckDB viewer writes it; the rule is in AVU_CONVENTION.md section 6, `transformJsonPath`), except a member that is itself a STRUCT, typed `sc:Text` without one; writes nothing |

`mesa_croissant_apply` in detail:

- **The new set.** `avus` are taken as given (only `croissant.*`, non-empty). A `document` is
  imported with `from_jsonld` in item mode (`item` = the path, its name, `is_folder`, and the
  stat size and MD5 of a file; `content_base` defaults to the item's folder): for a file, the
  FileObject that describes it becomes `croissant.file.*` and other distribution entries are
  reported. Then the values the generator makes anyway are left out, by the same rule as the
  portal's `croissant_service.drop_generated` (reimplemented against core): `croissant.url`
  equal to the default landing page or the DOI URL, `croissant.identifier` equal to the DOI
  URL (the DOI comes from the item's `datacite.identifier`), and any crosswalk property
  (name, description, license, creator, publisher, datePublished, version, keywords,
  temporalCoverage, spatialCoverage, `prov:wasGeneratedBy`, url, identifier) whose imported
  AVUs generate exactly the value the item's non-Croissant AVUs (or its name) give. Each drop
  is a warning naming the source.
- **merge** works per canonical path (`croissant.license` = `croissant.license.1`) and per
  entity: a path of the new set replaces its current values; an entity the new set gives a
  property of (`creator.N`, `distribution.N`, `recordSet.N`, `...field.M`, `...subField.K`)
  replaces the current entity of that index as a whole (its nested entries of a list the new
  set also gives go too, with their unknown keys); everything else stays, with a warning for
  kept entries of lists the new set gives. A property a document gave as its generated value also loses its current
  pinned `croissant.*` value, so the result matches the document.
- **replace** makes the item's `croissant.*` AVUs the new set plus the current unknown
  `croissant.*` keys and index-0 entries (convention rule 7), unless `drop_unknown`.
- In both modes an unknown key (or index-0 entry) belongs to the deepest entity its attribute
  reaches, as the codec binds it (`_entity_chain`, checked against `core._place`); when the
  diff removes that entity, or one above it, because of its known keys, the key is removed
  too (rule 7), so no entity of unknown keys only is left behind. An entity that has only
  unknown keys is not removed. `_canon` matches index segments with `fullmatch`, as core
  does.
- Non-`croissant.*` AVUs are never touched. Values are compared stripped (as iRODS stores
  what the tool writes). Import errors (non-`croissant.*` attributes, empty values, values over
  the limits, more than 5,000 AVUs) make a write fail with `croissant_invalid`, nothing
  written; if an add fails, the old value of that path is kept.

## Out of scope for version 1 (tracked as issues)

- Anonymous reading of Croissant on public paths and `<script type="application/ld+json">` on
  public pages for Google Dataset Search (needs a public AVU read path; today `GET
  /data/metadata/` is login-only).
- A `croissant.json` sidecar file written next to the data.
- Server-side Parquet/CSV schema inference (croissant-baker style) without the DuckDB viewer.
- Full `mlcroissant` validation inside the portal.
- Eclair / croissant-baker MCP interoperability beyond documented recipes.
