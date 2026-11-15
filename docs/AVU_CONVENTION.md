# MESA Croissant AVU convention (version 1)

This document is the contract between the MESA Portal, the MESA-MCP Croissant tools and
any pipeline that annotates CyVerse Data Store folders and files with
[MLCommons Croissant](https://docs.mlcommons.org/croissant/) metadata.

Croissant metadata lives in iRODS AVUs (attribute, value, unit) on the folder or file it
describes, in the same flat `namespace.key` style that MESA-MCP uses for `datacite.*`,
`mesa.*` and ontology AVUs. A Croissant JSON-LD document is **generated** from the AVUs
whenever it is needed (portal view, download, MCP export). AVUs are the source of truth.

Targets:

| Spec | `conformsTo` URI | Status |
|---|---|---|
| Croissant Core 1.1 | `http://mlcommons.org/croissant/1.1` | always emitted |
| Croissant RAI 1.0 | `http://mlcommons.org/croissant/RAI/1.0` | emitted when any `croissant.rai.*` AVU is present |
| GeoCroissant 1.0 | `http://mlcommons.org/croissant/geo/1.0` | emitted when any `croissant.geo.*` AVU is present |

Croissant 1.0 documents are accepted on import (1.0 `repeated`, `delimiter`, `jsonQuery`
are converted to their 1.1 forms).

## 1. General rules

1. **Single reserved prefix.** Every attribute starts with `croissant.` (lower case). RAI
   and GeoCroissant properties live under `croissant.rai.` and `croissant.geo.`. The prefix
   `croissant` is reserved: ontology detection (MESA-MCP `ols/transform.py`, portal
   `ols_transform.py` and the metadata page's `RESERVED_OLS_PREFIXES`) must not treat it as
   an ontology.
2. **Dotted paths.** An attribute is `croissant.` followed by dot-separated segments. A
   segment that is all digits is a **1-based index** into a repeated property; any other
   segment is a property name in the spelling of the Croissant spec (camelCase, no `cr:`/
   `sc:` prefix). Examples: `croissant.name`, `croissant.creator.2.name`,
   `croissant.recordSet.1.field.3.dataType`.
3. **Values are text.** Booleans are `true` / `false`. Numbers are decimal text. Dates are
   ISO 8601 (`2024-03-01`, `2024`, `2024-03-01T12:00:00Z`). A value is never empty (iRODS
   refuses empty values); an absent property is an absent AVU. A whitespace-only value counts
   as empty: readers ignore it with a warning (editors drop it on the next save) and importers
   leave it out with a warning. Values are at most 2,700 characters and units at most 250
   (portal and Terrain limits).

   **Whitespace.** Readers and the codec never trim a non-blank stored value: they show,
   compare and write back exactly what is stored (data-info compares AVUs exactly). Writers
   store *new* values and units trimmed of leading and trailing whitespace: the portal editor
   trims the text typed into its fields, and `mesa_croissant_apply` trims every attribute,
   value and unit it writes (mesa-mcp's iRODS helper strips every triple), with a warning
   naming each attribute it trimmed. Whitespace inside a value is kept, so a multi-line
   description keeps its line breaks and loses only a trailing newline. A value whose
   surrounding whitespace matters (a `column` name with padding) therefore cannot be written
   through these tools; `mesa_croissant_from_schema` warns about such a column.
4. **Units are empty** unless this document gives them a meaning:
   - `croissant.keyword.N`: optional CURIE or IRI of the term (makes a `DefinedTerm`), the
     same rule as MESA ontology AVUs (`unit = CURIE`). A keyword holds a name and one term
     identifier: importing a `DefinedTerm` keeps its `name` and its `termCode` (else `url`,
     else `@id`) and reports, with a warning, a `url` next to a `termCode` and an
     `inDefinedTermSet` (there is no key for them; a sub-key of the repeatable
     `croissant.keyword.N` would not follow its keyword when 1.3.0 readers renumber the
     list). A document generated with `keyword_style="text"` (what mlcroissant 1.1.1 loads)
     lists plain names, from these AVUs or the crosswalk (section 7): a `DefinedTerm`
     becomes its name, and a name already listed (compared case-insensitively) is left
     out, keeping the first spelling and the order.
   - `croissant.geo.spatialResolution`, `croissant.geo.temporalResolution`: unit text such as
     `m`, `km`, `s`, `day` (becomes `QuantitativeValue.unitText`).
   - `croissant.distribution.N.contentSize`: optional unit, default `B`.
5. **Indexes.** Writers number from 1 without gaps, without leading zeros (`1`, not `01`).
   Readers sort by index, accept gaps and leading zeros, and ignore index 0. A property
   listed as repeatable below may also be written without an index (`croissant.license` is
   read as `croissant.license.1`). `mesa_croissant_apply` refuses a new set that breaks the
   writer rule (index 0, `01`, `007`, `-1`, an empty segment such as `croissant..name`, or a
   gap such as `keyword.1` with `keyword.3`), with an error naming the attributes; AVUs
   already stored are read as above. Gaps are judged on what the item holds after the write:
   with `replace` that is the new set, with `merge` the new set plus the stored entries that
   stay. So a merge may give `creator.2` or `recordSet.2` alone when the item already has
   index 1; it is refused when it would leave a gap (a new index after a missing one, or an
   entry the merge removes before a new one). An entry already stored after an old gap may
   still be edited.
6. **One value per path.** Two AVUs with the same attribute are an error for scalar paths
   (and for the same index of a repeatable property, `croissant.license` next to
   `croissant.license.1` included); readers use the first in AVU order and report a
   warning, and `mesa_croissant_apply` refuses such a new set. Editors keep the extra values: they are written back on save (with
   their entity when the path is below one, see rule 7) until someone deletes them in
   the raw AVU view. Only the first value reaches the JSON-LD. An extra value at an index
   of a repeatable property belongs to the value it duplicates: when an editor renumbers
   the list (rule 5) it is written under that value's new attribute (`croissant.license.2`
   twice with no `license.1` is saved as `croissant.license.1` twice, a field's single
   `dataType` twice unindexed), so it never becomes a value of its own, and removing that
   value removes its duplicates too.
7. **Unknown keys are kept.** A `croissant.*` attribute this version does not define is
   preserved by every editor and reported as a warning by the validator. It is not emitted
   into the JSON-LD. An unknown key below an indexed entity (`croissant.creator.2.orcidVerified`,
   `croissant.recordSet.1.field.3.transform`) belongs to that entity: when an editor
   renumbers the entities (rule 5) it is written under the entity's new index, and removing
   the entity removes its unknown keys too. An entity that has only unknown keys keeps its
   index but is left out of the JSON-LD. Unknown keys that are not below an entity stay as
   they are.
8. **Where AVUs go.** Dataset-level keys go on the folder that is the dataset root (for a
   MESA DuckLake project, the folder carrying `mesa.enabled=true`). A file may carry
   dataset-level keys too (a single-file dataset) and/or `croissant.file.*` keys that
   describe the file itself.

## 2. Dataset (`sc:Dataset`)

| Attribute | Croissant property | Card. | Notes |
|---|---|---|---|
| `croissant.conformsTo.N` | `dct:conformsTo` | many | optional; Core 1.1 plus RAI/Geo URIs are added automatically |
| `croissant.name` | `name` | one | **required** |
| `croissant.alternateName` | `alternateName` | one | |
| `croissant.description` | `description` | one | **required** |
| `croissant.url` | `url` | one | **required**; defaults to the portal metadata page (or `https://doi.org/<doi>` when a DOI exists) |
| `croissant.license.N` | `license` | many | **required**; SPDX or Creative Commons URL recommended |
| `croissant.creator.N.<sub>` | `creator` | many | **required**; see 2.1 |
| `croissant.publisher.N.<sub>` | `publisher` | many | see 2.1 |
| `croissant.datePublished` | `datePublished` | one | **required** |
| `croissant.dateCreated` | `dateCreated` | one | |
| `croissant.dateModified` | `dateModified` | one | |
| `croissant.version` | `version` | one | semantic version recommended (`1.0.0`) |
| `croissant.citeAs` | `citeAs` | one | BibTeX recommended |
| `croissant.keyword.N` | `keywords` | many | unit = optional CURIE/IRI |
| `croissant.inLanguage.N` | `inLanguage` | many | BCP 47 (`en`) |
| `croissant.sameAs.N` | `sameAs` | many | URLs |
| `croissant.identifier` | `identifier` | one | e.g. a DOI URL |
| `croissant.isLiveDataset` | `isLiveDataset` | one | `true`/`false` |
| `croissant.sdVersion` | `sdVersion` | one | version of the metadata itself |
| `croissant.sdLicense` | `sdLicense` | one | license of the metadata |
| `croissant.temporalCoverage` | `temporalCoverage` | one | ISO 8601 interval `2018-01-01/2021-12-31` |
| `croissant.wasGeneratedBy` | `prov:wasGeneratedBy` | one | free text or URL (Croissant 1.1 provenance); a URL (or `urn:`) is emitted as `{"@id": …}`, text as `{"@type": "prov:Activity", "prov:label": …}` |
| `croissant.wasDerivedFrom.N` | `prov:wasDerivedFrom` | many | URLs; emitted as `{"@id": …}` (text as `{"@type": "prov:Entity", "prov:label": …}`) |

### 2.1 Persons and organizations (`creator`, `publisher`)

| Sub-key | Meaning |
|---|---|
| `name` | **required** for the entry |
| `type` | `Person` (default) or `Organization` |
| `email` | |
| `url` | |
| `identifier` | ORCID or ROR URL |
| `affiliation` | organization name (Person only) |

## 3. Responsible AI (`croissant.rai.*`, RAI 1.0)

Spelling follows the RAI 1.0 spec. Properties marked "many" are written `croissant.rai.<p>.N`;
the others are scalar `croissant.rai.<p>`.

| Property | Card. |
|---|---|
| `dataCollection` | one |
| `dataCollectionType` | many |
| `dataCollectionMissingData` | one |
| `dataCollectionRawData` | one |
| `dataCollectionTimeframe` | many (ISO 8601 date-times) |
| `dataImputationProtocol` | one |
| `dataManipulationProtocol` | one |
| `dataPreprocessingProtocol` | many |
| `dataAnnotationProtocol` | one |
| `dataAnnotationPlatform` | many |
| `dataAnnotationAnalysis` | many |
| `dataReleaseMaintenancePlan` | many |
| `personalSensitiveInformation` | many |
| `dataSocialImpact` | one |
| `dataBiases` | many |
| `dataLimitations` | many |
| `dataUseCases` | many |
| `annotationsPerItem` | one |
| `annotatorDemographics` | many |
| `machineAnnotationTools` | many |

Import also accepts the mlcroissant spellings `dataCollectionTimeFrame` and
`dataDataManipulationProtocol`, and RAI terms in the `cr:` namespace (Hugging Face output).

## 4. GeoCroissant (`croissant.geo.*`, GeoCroissant 1.0)

| Attribute | JSON-LD | Notes |
|---|---|---|
| `croissant.geo.spatialCoverage.name` | `spatialCoverage.name` | place name |
| `croissant.geo.spatialCoverage.box` | `spatialCoverage.geo` (`GeoShape.box`) | `"S W N E"` (lat/lon, space separated, schema.org order). Import also accepts commas |
| `croissant.geo.spatialCoverage.point` | `spatialCoverage.geo` (`GeoCoordinates`) | `"lat lon"` |
| `croissant.geo.coordinateReferenceSystem` | `geocr:coordinateReferenceSystem` | e.g. `EPSG:4326` |
| `croissant.geo.spatialResolution` | `geocr:spatialResolution` | number, unit = unit text (`m`); emitted as `{"@type": "sc:QuantitativeValue", "sc:value": 30, "unitText": "m"}` (`sc:value`: the bare term `value` is `cr:value` in the Croissant 1.1 context). Import accepts `value` and `sc:value` |
| `croissant.geo.temporalResolution` | `geocr:temporalResolution` | number, unit = unit text |
| `croissant.geo.samplingStrategy` | `geocr:samplingStrategy` | |
| `croissant.geo.spatialBias` | `geocr:spatialBias` | |
| `croissant.geo.recordEndpoint` | `geocr:recordEndpoint` | URL (e.g. OGC API Records) |
| `croissant.geo.bandConfiguration.totalBands` | `geocr:bandConfiguration.totalBands` | integer |
| `croissant.geo.bandConfiguration.band.N` | `geocr:bandConfiguration.bandNameList` | band names |

`croissant.temporalCoverage` (section 2) is a schema.org property and is not under `geo`.

## 5. Distribution (`cr:FileObject`, `cr:FileSet`)

On the dataset folder: `croissant.distribution.N.<sub>`.

| Sub-key | Applies to | Notes |
|---|---|---|
| `id` | both | **required**; the `@id` other entries refer to. Unique in the dataset |
| `type` | both | `FileObject` (default) or `FileSet` |
| `name` | both | defaults to `id` |
| `description` | both | |
| `contentUrl` | FileObject | absolute URL, or a path relative to the dataset root (`data/x.csv`), written as the plain path (`my dir/a b.csv`, not percent-encoded) |
| `encodingFormat` | both | MIME type (`text/csv`, `application/x-parquet`, `image/tiff`). When absent the generator takes the type of the `contentUrl` extension (a FileSet: of its first `includes` with a known extension), else `application/octet-stream`: mlcroissant requires an `encodingFormat` on every FileObject and FileSet |
| `contentSize` | FileObject | number of bytes; unit optional (default `B`) |
| `sha256` / `md5` | FileObject | hex digests |
| `containedIn.M` | both | `id`s of the FileObject(s)/FileSet(s) containing this one |
| `includes.M` / `excludes.M` | FileSet | glob patterns relative to the dataset root (`data/*.parquet`, `data/**/*.parquet`). mlcroissant 1.1.1 matches them with Python's `fnmatch`: `*` also matches `/`, and `**/` needs at least one more folder, so `data/**/*.parquet` alone misses `data/x.parquet`. To cover a folder at any depth, give both patterns (a file matching both is read once) |

A relative `contentUrl` is resolved against the dataset root when the document is
generated: public paths become `https://data.cyverse.org/dav-anon/<path>`, other paths
`https://data.cyverse.org/dav/<path>` (needs a CyVerse login); the path is percent-encoded
then. On import, a `contentUrl` below the dataset root becomes the decoded relative path
again, but only when generating gives back exactly the same URL (otherwise the absolute URL
is kept, e.g. one containing `%2F`). A percent-encoded relative `contentUrl` is decoded under
the same rule.

### 5.1 The file itself (`croissant.file.*`)

A file may describe itself with `croissant.file.{name,description,encodingFormat,sha256,md5}`.
When a document is generated **for that file**, it becomes the only FileObject (`@id` = the file
name, trimmed, with each run of whitespace replaced by `_` (`file` if empty); `contentUrl`,
`contentSize` and `md5` come from the Data Store stat when not given). Its fields default to
that FileObject as their source.

Importing a document **for a file** maps the FileObject that describes the file (the entry
whose `@id` is the file's `@id`, else whose `contentUrl` is the file, else the only entry) to
`croissant.file.*`: `description` and `sha256` always, `name` / `encodingFormat` / `md5` only
when they differ from the file name / the format of its extension (`application/octet-stream`
for an extension without a known type) / the stat MD5. `@id`,
`contentUrl` and `contentSize` are generated and not stored; a field `source` naming the file
is left out (fields default to it). Every other distribution entry is reported and dropped.

## 6. Record sets and fields (`cr:RecordSet`, `cr:Field`)

`croissant.recordSet.N.<sub>`:

| Sub-key | Notes |
|---|---|
| `id` | **required**; unique; field ids default to `<recordSet id>/<field name>` |
| `name` | defaults to `id` |
| `description` | |
| `dataType` | e.g. `cr:Split`, `sc:Enumeration` (optional) |
| `key.M` | field ids forming the key |
| `data` | inline JSON array of records (text, ≤ 2,700 characters) |
| `examples` | inline JSON (text) or URL |
| `field.M.<sub>` | see below |

`croissant.recordSet.N.field.M.<sub>`:

| Sub-key | Notes |
|---|---|
| `name` | **required** |
| `id` | defaults to `<recordSet id>/<name>`; unique in the dataset. Writers give an explicit `id` when the default contains whitespace: the default with each run of whitespace replaced by `_` (`mesa_croissant_from_schema` adds `-2`, `-3`, ... when that id is another field's default or already taken, so `a b` next to a column `a_b` gets `<recordSet id>/a_b-2`; a name used as given keeps its default, a trimmed or whitespace name yields to it) |
| `description` | |
| `dataType` / `dataType.K` | one or several types: `sc:Text`, `sc:Integer`, `sc:Float`, `sc:Boolean`, `sc:Date`, `sc:DateTime`, `sc:Time`, `sc:URL`, `sc:ImageObject`, `sc:GeoShape`, `sc:GeoCoordinates`, `cr:Int8…cr:Int64`, `cr:UInt8…cr:UInt64`, `cr:Float16/32/64`, `cr:BoundingBox`, `cr:Label`, `cr:Split`, `cr:SegmentationMask`, or any CURIE/IRI. The `sc:`/`cr:` prefix is optional on read. mlcroissant 1.1.1 reads a field's values with the first of its types it knows: `sc:Boolean`, `sc:Date`, `sc:DateTime`, `sc:Float`, `sc:Integer`, `sc:Text`, `sc:Time`, `sc:URL`, `cr:Int*`, `cr:UInt*`, `cr:Float*`, `sc:ImageObject`, `sc:AudioObject`, `sc:VideoObject`, `cr:BoundingBox`. A semantic type it does not read (`sc:GeoShape`, `sc:GeoCoordinates`, `sc:Number`, `sc:Enumeration`, `cr:Label`, `cr:Split`, `cr:SegmentationMask`, other CURIEs) should be given next to one it reads, e.g. `dataType.1 = sc:GeoShape`, `dataType.2 = sc:Text`; a field with none of them (and no subFields, and no field as its source) gets a validator warning, not an error |
| `source` | `id` of a distribution entry (FileObject or FileSet) or of another record set. With a record set and a `column` naming one of its fields the source is emitted as that field; a bare record-set source is valid Croissant 1.1 but mlcroissant 1.1 cannot load it (the validator warns) |
| `column` | `extract.column` (CSV, Parquet): the exact column name (the field `name` may be trimmed, the column is not) |
| `jsonPath` | `extract.jsonPath` |
| `fileProperty` | `extract.fileProperty`: `fullpath`, `filename`, `content`, `lines`, `lineNumbers` |
| `regex` | `transform.regex` |
| `separator` | `transform.separator` |
| `transformJsonPath` | `transform.jsonPath` (a member of a STRUCT / nested column, e.g. `bytes` of a Hugging Face image column). A plain member name is written bare only when it matches `[A-Za-z_][A-Za-z0-9_]*` and is not `where`. Any other name is written `$['<name>']` with `\` doubled and `'` written `\'` (mlcroissant's JSONPath parser cannot parse a bare `geo id` or `where`, so reading the record set fails). Emitted before the other transforms. Import maps `transform.jsonPath` here, and a 1.0 `transform.jsonQuery` too unless the field has no `column` and no `jsonPath` (then it is the 1.1 `extract.jsonPath`) |
| `format` | `source.format` (CLDR date or number pattern) |
| `references` | field id this field references (foreign key) |
| `isArray` | `true`/`false` |
| `arrayShape` | e.g. `-1` or `512,512,6` |
| `equivalentProperty.K` | URLs |
| `subField.K.<sub>` | nested fields, same sub-keys (one level of nesting is written by MESA tools). A subField that says how it is read (`source`, `column`, `jsonPath`, `fileProperty`, `transformJsonPath`, `regex`, `separator` or `format`) reads from its parent's source (the parent's own `source`, else what the parent inherits, e.g. the file in a file document) unless it names its own `source`; with `transformJsonPath` and no extract of its own it also reads its parent's `column`. A subField with none of these is read from its parent field's source and column: the validator warns (mlcroissant needs `transformJsonPath` on the member) but does not report an error |

## 7. MESA crosswalk (read-only defaults)

When a required or recommended Croissant property has no `croissant.*` AVU, the generator
fills it from AVUs that already exist, and the result marks the property's source. Writers
never copy these automatically; an editor may offer "copy into Croissant".

| Croissant | Taken from (first that exists) |
|---|---|
| `name` | `datacite.title.1.value`, `datacite.title`, `dc.title`, the folder/file name |
| `description` | `datacite.description.1.value`, `datacite.description`, `dc.description` |
| `creator` | `datacite.creator.N.{name,nameType,orcid,nameIdentifier,affiliation}`, `datacite.creator`, `dc.creator` |
| `publisher` | `datacite.publisher`, `dc.publisher` |
| `license` | `datacite.rights.N.rightsURI`, `datacite.rights.N.value` when it is a URL, `dc.rights` when it is a URL |
| `datePublished` | `datacite.publicationYear`, `datacite.date.N` with `dateType=Issued`, `dc.date` |
| `version` | `datacite.version` |
| `identifier`, `url` | `datacite.identifier` with `identifierType=DOI` → `https://doi.org/<doi>` |
| `keywords` | `datacite.subject.N.value` in index order, then ontology AVUs (`<ontology>.<key>` whose unit is a CURIE) as `DefinedTerm`s (`termCode` = the unit). The term's `name` is its label when a subject names the term: its `classificationCode` or `valueURI` is the CURIE (or its OBO PURL `http://purl.obolibrary.org/obo/<PREFIX>_<id>`), or its value ends with `(<CURIE>)`, as in `area of deciduous forest (ENVO:01000816)` (label: the value without that suffix). That `DefinedTerm` then takes the subject's place in the list, so the term is not listed twice. Without such a subject the `name` is the AVU value, which may be free text: the attribute key is not used, since an editor that picks a sub-term keeps the parent field's key (`envo.biome` with a sub-term's label and CURIE). A term given by several AVUs is listed once |
| `temporalCoverage` | `neon.temporalCoverage`, `datacite.date.N` with `dateType=Collected` |
| `spatialCoverage` | `datacite.geoLocation.N.box` (`S,W,N,E` → `S W N E`), `datacite.geoLocation.N.point`, `datacite.geoLocation.N.place`; else the flat (legacy) `datacite.geoLocationBox`, `datacite.geoLocationPoint`, `datacite.geoLocationPlace` (the indexed keys take precedence) |
| `prov:wasGeneratedBy` | `mesa.generated_by` (a URL becomes `{"@id": …}`) |

## 8. MESA DuckLake projects

A folder with `mesa.enabled=true` that is a registered DuckLake (portal
`lake_registry.json`) gets these **suggestions** (not automatic AVUs):

- a FileObject for the catalog file (`encodingFormat` `application/x-ducklake`, an
  unregistered media type used by MESA, `contentUrl` relative to the root);
- a FileSet per table `ducklake-<table>` with `encodingFormat` `application/x-parquet` and
  `includes` `<data path>/<schema>/<table>/*.parquet` and
  `<data path>/<schema>/<table>/**/*.parquet` (an unpartitioned table keeps its files
  directly in the table folder, a partitioned one in `k=v/` folders; see section 5 for why
  both). FileSets written with only the `**/` pattern (core 1.3.0) stay valid but find no
  files of an unpartitioned table in mlcroissant 1.1.1;
- from the portal DuckDB viewer, one RecordSet per table with one Field per column
  (`source` = the table's FileSet, `column` = the column name, `dataType` from the DuckDB type
  map in section 9).

## 9. DuckDB / Arrow type map

| DuckDB / Arrow type | Croissant `dataType` |
|---|---|
| BOOLEAN | `sc:Boolean` |
| TINYINT, SMALLINT, INTEGER, BIGINT | `cr:Int8`, `cr:Int16`, `cr:Int32`, `cr:Int64` |
| UTINYINT, USMALLINT, UINTEGER, UBIGINT | `cr:UInt8`, `cr:UInt16`, `cr:UInt32`, `cr:UInt64` |
| HUGEINT, UHUGEINT, DECIMAL(p,s) | `sc:Float` |
| FLOAT (REAL), DOUBLE | `cr:Float32`, `cr:Float64` |
| DATE | `sc:Date` |
| TIME, TIMETZ | `sc:Time` |
| TIMESTAMP*, TIMESTAMPTZ | `sc:DateTime` |
| VARCHAR, UUID, INTERVAL, BLOB, JSON, ENUM, anything else | `sc:Text` (JSON is read as its text) |
| GEOMETRY, WKB_BLOB | `dataType.1 = sc:GeoShape`, `dataType.2 = sc:Text`: Parquet stores the WKB bytes, which mlcroissant 1.1.1 reads as text (bytes); it cannot read `sc:GeoShape` alone |
| POINT_2D, BOX_2D, LINESTRING_2D, POLYGON_2D (spatial extension) | their Parquet layout, as for STRUCT: POINT_2D is `sc:GeoCoordinates` with subFields `x`, `y` (`cr:Float64`); BOX_2D is `sc:GeoShape` with `min_x`, `min_y`, `max_x`, `max_y`; LINESTRING_2D is `sc:GeoShape`, `isArray`, subFields `x`, `y`; POLYGON_2D the same with `arrayShape` `-1,-1` (mlcroissant 1.1.1 does not read subFields of a list of lists; `mesa_croissant_from_schema` warns) |
| `MAP(K, V)` | no dataType; `isArray=true` (one more `-1` in `arrayShape`) with the subFields `key` (type of `K`, `transformJsonPath` `$[0]`) and `value` (type of `V`, `transformJsonPath` `$[1]`). mlcroissant (pandas) reads a Parquet MAP as a list of (key, value) pairs; as `sc:Text` it fails the whole record set |
| `T[]`, `LIST(T)`, `T[n]` | dataType of `T` with `isArray=true` (`arrayShape` `-1` or `n`) |
| STRUCT(...) | no dataType on the field; one `subField` per member. Writers should give each member `transformJsonPath` = the member's JSONPath: the bare name for a plain identifier, else `$['…']` (section 6, `transformJsonPath`); it then reads the parent's source and column, and mlcroissant loads it. A member that is itself a STRUCT is typed `sc:Text` (one level of nesting) and gets no `transformJsonPath` (with one, mlcroissant 1.1.1 fails the whole record set casting the object to text; without it the member reads as null). A member without it is read from its parent's source and column and the validator warns, without an error |

## 10. Example

AVUs on `/iplant/home/shared/mesa/demo`:

```
croissant.name                         = Demo ratings
croissant.description                  = Ratings of movies by users.
croissant.license.1                    = https://creativecommons.org/licenses/by/4.0/
croissant.creator.1.name               = Jane Doe
croissant.creator.1.identifier         = https://orcid.org/0000-0000-0000-0000
croissant.datePublished                = 2026-11-06
croissant.keyword.1                    = grassland        [unit ENVO:00000106]
croissant.distribution.1.id            = ratings-csv
croissant.distribution.1.contentUrl    = ratings.csv
croissant.distribution.1.encodingFormat= text/csv
croissant.recordSet.1.id               = ratings
croissant.recordSet.1.field.1.name     = user_id
croissant.recordSet.1.field.1.dataType = sc:Integer
croissant.recordSet.1.field.1.source   = ratings-csv
croissant.recordSet.1.field.1.column   = userId
```

generates

```json
{
  "@context": { "...": "the full Croissant 1.1 context plus rai, geocr, prov" },
  "@type": "sc:Dataset",
  "conformsTo": "http://mlcommons.org/croissant/1.1",
  "name": "Demo ratings",
  "description": "Ratings of movies by users.",
  "license": "https://creativecommons.org/licenses/by/4.0/",
  "url": "https://mesa.cyverse.org/data/metadata/page/?path=/iplant/home/shared/mesa/demo&type=folder",
  "creator": {"@type": "sc:Person", "name": "Jane Doe", "identifier": "https://orcid.org/0000-0000-0000-0000"},
  "datePublished": "2026-11-06",
  "keywords": [{"@type": "sc:DefinedTerm", "name": "grassland", "termCode": "ENVO:00000106"}],
  "distribution": [{
    "@type": "cr:FileObject", "@id": "ratings-csv", "name": "ratings-csv",
    "contentUrl": "https://data.cyverse.org/dav-anon/iplant/home/shared/mesa/demo/ratings.csv",
    "encodingFormat": "text/csv"
  }],
  "recordSet": [{
    "@type": "cr:RecordSet", "@id": "ratings", "name": "ratings",
    "field": [{
      "@type": "cr:Field", "@id": "ratings/user_id", "name": "user_id", "dataType": "sc:Integer",
      "source": {"fileObject": {"@id": "ratings-csv"}, "extract": {"column": "userId"}}
    }]
  }]
}
```

## 11. Versioning of this convention

This is version 1. A future incompatible change adds `croissant.avuConvention = 2` on the
annotated item; its absence means version 1.
