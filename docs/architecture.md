# Architecture

## Target pipeline (stable shape - do not change casually)
config (YAML) -> orchestration -> generators -> impurity engine (optional) -> writers -> run manifest

## Current state (end of Milestone 4b)
config/pipeline.yaml
    | [CONFIG]
    v
src/core/config.py           typed, validated config (ADR-0008)
    | [SEED]
    v
src/core/seed_manager.py     master seed -> child seed per entity (ADR-0006)
    | [GENERATION]
    v
generators in FK order (ADR-0014), fed context by main.py (ADR-0015)
    | [WRITER]
    v
src/writers/__init__.py      get_writer(format) factory (ADR-0017)
    |                             |
    v                             v
CsvWriter (csv)          JsonWriter (ndjson)     ParquetWriter (parquet)
                              all extend Writer ABC
    | [MANIFEST]
    v
src/metadata/manifest.py     data/generated/_manifests/run_<run_id>.json (ADR-0011)

## Data flow rules
- Generators never import writers; writers never import generators.
- main.py is the ONLY module that wires stages, validates composition, and
  builds the generation context.
- main.py imports the writer FACTORY, never a concrete writer (ADR-0017).
- Serialization policies live in src/writers/serialization.py: text formats
  stringify dates and Decimal values; Parquet preserves their native types
  (ADR-0018).

## Module responsibilities
| Module                     | Responsibility                                        |
|----------------------------|-------------------------------------------------------|
| main.py                    | Wiring, composition validation, context building      |
| src/core/                  | paths, config, seeds, logging (infrastructure)        |
| src/models/                | Data contracts (frozen dataclasses, SCHEMA_VERSION)   |
| src/generators/            | Record streams; know nothing about files              |
| src/writers/base.py        | Generic Writer ABC + file lifecycle (ADR-0017/0018)   |
| src/writers/serialization  | Shared text and native serialization policies         |
| src/writers/*_writer.py    | Format implementations                          |
| src/metadata/              | Run manifests and audit info                          |

## Logging
Contract: ADR-0009. Stages: STARTUP, CONFIG, SEED, GENERATION, WRITER, MANIFEST, SUMMARY.
Crashes logged via run() with exit codes 0/1/130.

## Planned (do not create early)
src/quality, src/scenarios, src/streaming, src/orchestration
