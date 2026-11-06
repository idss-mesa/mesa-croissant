# mesa-croissant

[MLCommons Croissant](https://docs.mlcommons.org/croissant/) metadata for the MESA
ecosystem. Folders and files in the CyVerse Data Store carry Croissant metadata as
`croissant.*` iRODS AVUs. The MESA Portal shows and edits it in a **Croissant** tab, and agents
write the same AVUs through MESA-MCP tools. A Croissant JSON-LD document is generated from the
AVUs whenever it is needed. The AVUs are the source of truth.

Targets: Croissant Core 1.1, Croissant RAI 1.0 and GeoCroissant 1.0. Croissant 0.8 and 1.0
documents are accepted on import.

- Contract: [`docs/AVU_CONVENTION.md`](docs/AVU_CONVENTION.md) (version 1)
- Design: [`docs/DESIGN.md`](docs/DESIGN.md)
- Roadmap: [`docs/ROADMAP.md`](docs/ROADMAP.md)

## Components

| Component | Where | What |
|---|---|---|
| `mesa_croissant.core` | `src/mesa_croissant/core.py` | Pure-stdlib codec: AVUs ⇄ nested model ⇄ Croissant JSON-LD, MESA crosswalk (DataCite / Dublin Core / ontology AVUs), importer, structural validator, encoding-format and DuckDB type maps. No I/O, no third-party imports. |
| `mesa_croissant.mcp_tools` | `src/mesa_croissant/mcp_tools.py` | MESA-MCP plugin: the `mesa_croissant_*` tools (surface `croissant`), loaded through the `mesa_mcp.tools` entry point. |
| Portal service | `mesa-portal/portal/services/croissant_core.py` | Vendored, byte-identical copy of `core.py`. |
| Portal glue and UI | `mesa-portal/portal/services/croissant_service.py`, `portal/views/croissant_views.py`, the metadata page's Croissant tab | Reads AVUs, stat and listings through `DataService`, and adds portal URLs. |
| Golden fixtures | `tests/fixtures/` | AVU lists with their expected JSON-LD, plus upstream documents for import. The portal keeps a copy in `tests/fixtures/croissant/`. |

### MESA-MCP tools

| Tool | Input | Effect |
|---|---|---|
| `mesa_croissant_template` | `{level}`: `dataset`, `rai`, `geo`, `file`, `distribution`, `recordset` or `all` | Returns the key catalog: attribute, cardinality, required, unit, description. |
| `mesa_croissant_export` | `{path, url?, keyword_style?}` | Reads the item's AVUs (and a file's stat size and MD5), runs `to_jsonld` and returns `{document, sources, validation, croissant_avu_count}`. |
| `mesa_croissant_validate` | `{document}` or `{path}` | Returns the `validate` report. |
| `mesa_croissant_apply` | `{path, document? \| avus?, mode: merge\|replace, drop_unknown=false, dry_run=true, content_base?}` | Imports the document (or takes the AVUs), diffs the new AVUs against the item's `croissant.*` AVUs and returns `{add, remove, unchanged}` with the validation of the resulting document. Unless `dry_run`, writes the diff and mirrors it into the DuckLake AVU history as one snapshot. |
| `mesa_croissant_from_schema` | `{columns: [{name, type}], record_set_id, source_id, record_set_index=1, record_set_name?, description?}` | Builds record-set AVUs from a DuckDB or Arrow schema. STRUCT members get `transformJsonPath` = the member name (`$['…']` unless a plain identifier), so mlcroissant loads them; a member that is itself a STRUCT is typed `sc:Text` without one. Writes nothing. |

How `mesa_croissant_apply` behaves:

- **Modes.** `merge` works per entry on canonical paths (`croissant.license` and
  `croissant.license.1` are the same path, as are a field's `dataType` and `dataType.1`). A
  path of the new set replaces its current value. An entity the new set gives a property of
  (`creator.N`, `publisher.N`, `distribution.N`, `recordSet.N`, `recordSet.N.field.M`,
  `...subField.K`) replaces the current entity of that index as a whole: its other
  properties are removed, and so are its nested entries of a list the new set also gives
  (`recordSet.1.field.3` when the new `recordSet.1` has an id and fields 1-2). Any other
  `croissant.*` AVU stays, and a warning names kept entries of lists the new set gives and
  kept nested entries of a replaced entity. To change one property of an entity, give the
  whole entity (from `mesa_croissant_export`) and check the dry run. `replace` makes the item's `croissant.*` AVUs the
  new set plus the current unknown `croissant.*` keys and index-0 entries (convention rule 7;
  a document cannot carry them), unless `drop_unknown=true`. In both modes an unknown key
  (or index-0 entry) below an entity the diff removes goes with it (rule 7), so no entity of
  unknown keys only is left behind; the warning on that entity says so. An entity that
  already has only unknown keys stays. Non-`croissant.*` AVUs are never touched in either
  mode.
- **Whitespace.** Values are compared the way iRODS stores what the tool writes (stripped),
  so a stored value that differs only by surrounding whitespace is unchanged. Removes use the
  exact stored triple.
- **Dry run.** `dry_run` is true by default.
- **Write order.** Adds are written before removes. If an add fails, the old value of that
  path is kept. If the new set has import errors (non-`croissant.*` attributes, empty
  values, values over 2,700 characters, more than 5,000 AVUs), the tool writes nothing and
  returns `croissant_invalid`.
- **Files.** A document applied to a file is imported in item mode: the FileObject that
  describes the file becomes `croissant.file.*` (its `@id`, `contentUrl`, `contentSize`,
  default `encodingFormat` and an `md5` equal to the stat checksum are left out), and other
  distribution entries are reported as warnings. A file's exported document therefore applies
  back as a no-op.
- **Generated values.** A document exported for the same path carries values the generator
  makes: the default landing page or the DOI URL as `url`, the DOI URL as `identifier`, the
  item name, and whatever the crosswalk takes from `datacite.*`, `dc.*` or ontology AVUs.
  Apply leaves those out, with a warning naming the source, so a later DataCite edit or DOI
  still reaches the document. The rule (`drop_generated`) is the one the portal's JSON-LD
  import applies, so both importers store the same AVUs. In `merge` mode the item's current
  pinned value of such a property is removed too.
- **Unknown keys and duplicates.** Unknown `croissant.*` keys and index-0 entries are kept
  by both modes (unless `replace` with `drop_unknown=true`), except below an entity the diff
  removes; a document cannot carry them.
  Extra values of a duplicated path are not unknown keys: a path the new set gives replaces
  all its current values, and `replace` removes the duplicates of paths it does not give.

## Status

| Part | Status |
|---|---|
| AVU convention v1 (`docs/AVU_CONVENTION.md`) | done |
| `core.py` codec, generator, importer, validator, type maps | done, `CORE_VERSION` 1.2.0 |
| Golden fixtures and upstream import fixtures (pinned mlcommons examples) | done |
| mlcroissant 1.1.1 compatibility tests (optional) | done; see the caveats below |
| MESA-MCP plugin (`mesa_croissant_*`) | done |
| `croissant` reserved prefix in mesa-mcp (`ols/transform.py`, `_tool_surface`) | branch `feat/croissant-reserved-prefix` of idss-mesa/mesa-mcp |
| Portal: vendored core, Croissant tab, endpoints, folder-header chip, DuckDB hand-off | in progress (mesa-portal branch `feat/croissant`) |
| Items out of scope for version 1 | [`docs/ROADMAP.md`](docs/ROADMAP.md) |

## Using the core

```python
from mesa_croissant import from_jsonld, parse_avus, to_jsonld, validate

avus = [
    {"attribute": "croissant.name", "value": "Demo ratings", "unit": ""},
    {"attribute": "croissant.keyword.1", "value": "grassland", "unit": "ENVO:00000106"},
]
out = to_jsonld(avus, item={"path": "/iplant/home/shared/mesa/demo", "name": "demo"})
out["document"]      # Croissant 1.1 JSON-LD
out["sources"]       # where each property came from ("croissant", "datacite.title.1.value", ...)
out["validation"]    # {"valid", "errors", "warnings", "conformsTo", "summary"}

back = from_jsonld(out["document"])   # {"avus", "warnings", "errors"}
```

`parse_avus` gives the nested model editors work on. Unknown `croissant.*` keys go to its
`unknown` list, except below an indexed entity (`croissant.creator.2.orcidVerified`), where
they stay with the entity as `entity[core.UNKNOWN_KEY]` (`"__unknown"`) and `model_to_avus`
writes them back under the entity's new index. Extra values of a duplicated path are kept the
same way, with a warning; only the first one reaches the JSON-LD. `UNKNOWN_KEY`, `VALUE_MAX`
and `UNIT_MAX` are imported from `mesa_croissant.core`.

`core.py` has no dependencies, so `pip install .` is enough. Python 3.11 or later is required.

## How the portal vendors `core.py`

The portal does not depend on this package. Instead, `src/mesa_croissant/core.py` is copied
**byte-identical** to `mesa-portal/portal/services/croissant_core.py`, and the golden fixtures
are copied to `mesa-portal/tests/fixtures/croissant/`. The portal's
`tests/test_croissant_core.py` pins `EXPECTED_CORE_VERSION` and runs the same golden cases.

To change the codec:

1. Edit `core.py` here (never the portal copy), add or adjust the tests and fixtures, and bump
   `CORE_VERSION`. A change to the AVU layout also needs a convention version bump (see
   [CONTRIBUTING.md](CONTRIBUTING.md)).
2. Copy `core.py` and `tests/fixtures/` into the portal and update `EXPECTED_CORE_VERSION`.
3. Run the portal's Croissant tests. `cmp src/mesa_croissant/core.py
   ../mesa-portal/portal/services/croissant_core.py` must report no difference.

## Installing the MESA-MCP plugin

mesa-mcp discovers the tools through this package's entry point:

```toml
[project.entry-points."mesa_mcp.tools"]
croissant = "mesa_croissant.mcp_tools"
```

Install mesa-croissant into the **same environment** as mesa-mcp, then restart the server.
mesa-mcp is not on PyPI, so install it first, either from its checkout or from git:

```bash
pip install "mesa-mcp @ git+https://github.com/idss-mesa/mesa-mcp"   # if it is not installed yet
pip install "mesa-croissant[mcp] @ git+https://github.com/idss-mesa/mesa-croissant"
# or, from a checkout:  pip install -e ".[mcp]"
```

At start-up, `MesaServer` calls `load_plugins()`, which imports `mesa_croissant.mcp_tools`.
The five tools then show up in `tools/list` with `_meta["io.mesa/surface"] == "croissant"`.

- **Broken plugin.** If the plugin fails to import, it is skipped with a `plugin.load_failed`
  warning. Set `MESA_MCP_SERVER__STRICT_PLUGINS=1` to make that fatal.
- **mesa-mcp version.** The plugin needs a mesa-mcp with the entry-point loader (PR #6, in
  0.1.0 at `c74f3aa` and in 0.2.0).
- **Reserved prefix.** The `croissant` prefix is reserved in mesa-mcp's ontology detection on
  the `feat/croissant-reserved-prefix` branch. Until that branch is merged, `croissant.*`
  keyword AVUs may be listed as an ontology by `detect_ontology_prefixes`.

Writes go through mesa-mcp's own helpers: `assert_allowed`, the per-user iRODS pool,
`add_avu_to_irods`, and one `record_avu_changes` snapshot with source
`mesa-mcp:mesa_croissant_apply`. That means path policy and DuckLake mirroring behave exactly
as they do for `ds_add_avus` and `mesa_avu_apply_datacite`. Removes call python-irodsclient's
`session.metadata.remove` with the exact stored triple (`delete_avu_from_irods` strips the
triple, so it could remove a different AVU).

## Validating with mlcroissant

The server-side check is `core.validate`, a structural validator with no dependencies. For the
full MLCommons check, use [`mlcroissant`](https://github.com/mlcommons/croissant/tree/main/python/mlcroissant)
(it pulls in pandas, rdflib and networkx, so it is not a dependency here):

```bash
pip install mlcroissant
mlcroissant validate --jsonld demo.croissant.json
```

```python
import mlcroissant as mlc
ds = mlc.Dataset(jsonld="demo.croissant.json")
for record in ds.records(record_set="ratings"):
    print(record)
```

Known mlcroissant 1.1.1 differences (see `tests/test_mlcroissant_compat.py`):

- **Keywords.** mlcroissant rejects keywords written as `sc:DefinedTerm`, although Croissant
  1.1 allows them. The crosswalk turns ontology AVUs into DefinedTerm keywords, so most MESA
  datasets have them. Export with `keyword_style="text"` (`item["keyword_style"]` in
  `to_jsonld`, or the `keyword_style` input of `mesa_croissant_export`) to get plain keyword
  names that mlcroissant loads.
- **Checksums.** mlcroissant requires `md5` or `sha256` on every FileObject unless
  `isLiveDataset` is true. For a file, the export takes the MD5 from the Data Store stat
  (`sha2:` checksums are not used yet). For distribution entries, add
  `croissant.distribution.N.md5` (or `sha256`). `core.validate` warns when both are missing.
- **STRUCT columns.** mlcroissant loads a subField of a STRUCT column only with
  `transform.jsonPath` (`croissant.…subField.K.transformJsonPath`).
  `mesa_croissant_from_schema` and the portal's DuckDB viewer write it for every member
  except one that is itself a STRUCT: that member is typed `sc:Text` and gets none, because
  mlcroissant 1.1.1 then fails the whole record set casting the object to text (without
  the transform the member reads as null). `tests/fixtures/schema_parity.json` holds one
  schema and the record set both writers build from it; `test_mlcroissant_compat.py` loads
  its records.
- **schema.org.** Documents use `https://schema.org/`, as every upstream example does.
  mlcroissant 1.1.1 does not find the Dataset in a document that uses `http://`. Import
  accepts both.
- **Warnings.** mlcroissant warns about a missing `citeAs` and `version`, and about context
  terms it adds itself (`path`, `repeated`, `replace`, `samplingRate`).

Running the compatibility tests: `pip install mlcroissant pytest` and then `pytest
tests/test_mlcroissant_compat.py`. They skip themselves when mlcroissant is absent.

## The Croissant ecosystem

**Specifications**
- [Croissant 1.1 specification](https://docs.mlcommons.org/croissant/docs/croissant-spec-1.1.html) ([source](https://github.com/mlcommons/croissant/tree/main/docs))
- [Croissant RAI 1.0 (Responsible AI)](https://docs.mlcommons.org/croissant/docs/croissant-rai-spec.html)
- [GeoCroissant 1.0](https://docs.mlcommons.org/croissant/docs/croissant-geo-spec.html)
- [MLCommons Croissant working group](https://mlcommons.org/working-groups/data/croissant/)

**Tools**
- [mlcroissant](https://github.com/mlcommons/croissant/tree/main/python/mlcroissant): the reference Python library and validator.
- [croissant-baker](https://github.com/MIT-LCP/croissant-baker) ([docs](https://lcp.mit.edu/croissant-baker/)): generates Croissant from local files (CSV, Parquet, images and more), with a `validate` subcommand and an MCP mode.
- [Eclair](https://eclair-docs.github.io/) ([code](https://github.com/mlcommons/croissant/tree/main/eclair)): an MCP server that helps LLMs find and use Croissant datasets.
- [ZOO-Project dcai](https://github.com/ZOO-Project/dcai) ([site](https://zoo-project.github.io/dcai/)): GeoCroissant recipes and converters (STAC, GeoDCAT, Zarr, GeoParquet and more).
- [Croissant editor](https://huggingface.co/spaces/marcenacp/croissant-editor).

**Platform endpoints**
- [Hugging Face](https://huggingface.co/docs/dataset-viewer/en/croissant): `GET https://huggingface.co/api/datasets/<org>/<name>/croissant`. Only for datasets the viewer can convert to Parquet.
- Kaggle: `https://www.kaggle.com/datasets/<owner>/<name>/croissant/download`, or "Export metadata as Croissant" on the dataset page.
- [OpenML](https://docs.openml.org/data/use/): the Croissant button on each dataset page, or the `<parquet url without .pq>_croissant.json` naming scheme.

Any of these documents can be imported with `from_jsonld`, the portal's "Import JSON-LD" or
`mesa_croissant_apply(document=...)`.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"            # core tests; the plugin tests skip without mesa-mcp
pip install "mesa-mcp @ git+https://github.com/idss-mesa/mesa-mcp@v0.2.0"   # plugin tests
pytest
ruff check .
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT, see [LICENSE](LICENSE). The upstream example documents in `tests/fixtures/upstream/` come
from the MLCommons Croissant repository under Apache-2.0 (see the `NOTICE` there).
