# Contributing

## Tests and lint

```bash
pip install -e ".[dev]"
pip install "mesa-mcp @ git+https://github.com/idss-mesa/mesa-mcp@v0.2.0"  # for tests/test_mcp_tools.py
pytest            # tests/test_mcp_tools.py skips without mesa-mcp, the mlcroissant tests without mlcroissant
ruff check .
```

- CI runs ruff and pytest on Python 3.11 and 3.12. It runs once without mesa-mcp, to prove the
  core needs no dependencies, and once with mesa-mcp for the plugin tests.
- Tests never call iRODS, Terrain, DuckLake or any network service. The plugin tests
  monkeypatch mesa-mcp's helpers on `mesa_croissant.mcp_tools`.
- A behaviour change to the generator or the importer updates the golden fixtures in
  `tests/fixtures/` deliberately, with the reason in the commit message.

## `core.py` stays vendorable

`src/mesa_croissant/core.py` is copied byte-identical into the MESA Portal
(`portal/services/croissant_core.py`), so:

- It uses the Python standard library only, does no I/O, and keeps Python 3.11 syntax.
- It is never edited in the portal. Change it here and bump `CORE_VERSION` (semver). Then copy
  it, and the golden fixtures, into the portal and update the portal's `EXPECTED_CORE_VERSION`.
- MESA-MCP-specific code belongs in `mcp_tools.py`, never in `core.py`.

## Convention changes

[`docs/AVU_CONVENTION.md`](docs/AVU_CONVENTION.md) is the contract between the portal, MESA-MCP
and every pipeline that writes `croissant.*` AVUs.

- **Compatible additions** need a `CORE_VERSION` minor bump and an update to the convention
  text. Examples: a new optional key, or a new import dialect.
- **Incompatible changes** need a convention version bump: `CONVENTION_VERSION` and the
  `croissant.avuConvention` value that section 11 describes. They also need a migration note.
  Examples: renaming or re-indexing keys, or changing a value format.
- Every change to the reserved `croissant` prefix, or to the units with a meaning, is
  mirrored in mesa-mcp (`ols/transform.py`) and in the portal (`ols_transform.py`,
  `RESERVED_OLS_PREFIXES`).
