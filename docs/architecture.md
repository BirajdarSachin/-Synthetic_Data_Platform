# Architecture

## Target pipeline (stable shape - do not change casually)
config (YAML) -> orchestration -> generators -> impurity engine (optional) -> writers -> run manifest

## Current state (end of Milestone 6a - window engine, platform 1.1.0)
config/pipeline.yaml
    | [CONFIG]
    v
src/core/config.py           typed, validated config (ADR-0008)
    | [SEED]
    v
src/core/seed_manager.py     child seeds: entities + quality rules (ADR-0006/0021)
    | [GENERATION]
    v
generators in FK order (ADR-0014), fed context by main.py (ADR-0015)
    | context extracted from CLEAN records only
    v
src/core/windows.py          window keys from business dates (ADR-0024)
    | bucketing: one deterministic stream -> event_date= window files
    v
src/quality/engine.py        ImpurityEngine per window stream (ADR-0021..0024) [QUALITY]
    |  1. null  2. invalid FK  3. out-of-range  4. drift  5. duplicate (last)
    | per-window seed scope: {entity}:{window}:quality:{rule}
    v
main.py path builders:
    snapshot: {entity}/reference_date={date}/{entity}_{date}.{fmt} (ADR-0019)
    windowed: {entity}/event_date={window}/{entity}_{window}.{fmt} (ADR-0024)
    |
src/writers/__init__.py      get_writer(format) factory (ADR-0017)
    |
CsvWriter / JsonWriter / ParquetWriter - Writer ABC, ATOMIC (ADR-0020)
    | [MANIFEST]
    v
src/metadata/manifest.py     manifest v3: layout + files[] (ADR-0011/0019/0024)

## Windowing rules (ADR-0024)
- Fact entities (customer, employee, order, order_item, payment, review) are
  windowed by business date; order_item via its order's date.
- Dimension entities (supplier, product) are ALWAYS snapshots.
- Temporal integrity across windows is inherited from ADR-0016 rules
  (order_date >= registration_date implies window order >= window customer).
- PK continuity: the union of all window files of an entity is exactly 1..N.
- mode="latest" produces the incremental file shape (one window per run).
- Corruption is applied per window stream AFTER clean bucketing.

## Data flow rules
- Generators never import writers or src/quality; src/quality imports neither.
- Corruption is LOCAL: context for child entities is always built from clean
  records (ADR-0015/0021).
- Absent quality config = byte-identical clean output. Absent window config
  = single snapshot per entity (pre-6a behavior).

## Module responsibilities
| Module                     | Responsibility                                        |
|----------------------------|-------------------------------------------------------|
| main.py                    | Wiring, validation, context, paths, bucketing         |
| src/core/                  | paths, config, seeds, windows, logging                |
| src/models/                | Data contracts (frozen dataclasses, SCHEMA_VERSION)   |
| src/generators/            | Clean record streams; know nothing about corruption   |
| src/quality/               | Impurity engine: fixed rule chain of stream transforms|
| src/writers/               | Serialization + atomic file lifecycle                 |
| src/metadata/              | Run manifests (v3) and audit info                     |

## Logging
Contract: ADR-0009. Stages: STARTUP, CONFIG, SEED, GENERATION, QUALITY, WRITER, MANIFEST, SUMMARY.

## Planned (do not create early)
6b: incremental INSERT/UPDATE/DELETE change stream, CDC-style log.
Phase 7: per-window independent generation, streaming context, multiprocessing.
