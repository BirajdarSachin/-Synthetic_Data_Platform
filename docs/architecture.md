# Architecture

## Target pipeline (stable shape - do not change casually)
config (YAML) -> orchestration -> generators -> impurity engine (optional) -> writers -> run manifest

## Current state (end of Milestone 5c - Phase 4 complete, platform 1.0.0)
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
src/quality/engine.py        ImpurityEngine - fixed rule chain (ADR-0021..0023) [QUALITY]
    |  1. null  2. invalid FK  3. out-of-range  4. schema drift  5. duplicate (last)
    | passthrough when no quality config
    v
main.py path builder: {entity}/reference_date={date}/{entity}_{date}.{fmt} (ADR-0019)
    |
src/writers/__init__.py      get_writer(format) factory (ADR-0017)
    |
CsvWriter / JsonWriter / ParquetWriter - Writer ABC, ATOMIC (ADR-0020)
    | [MANIFEST]
    v
src/metadata/manifest.py     manifest v2 + full quality record (ADR-0011/0019/0021)

## Data flow rules
- Generators never import writers or src/quality; src/quality imports neither.
- Corruption is LOCAL: context for child entities is always built from clean
  records (ADR-0015/0021).
- Quality rules get their own child seeds: {entity}:quality:{rule}.
- Absent quality config = byte-identical clean output.
- quality runs must target a directory other than the clean baseline (guard).
- Duplicates make written count exceed generated count; manifest records the
  WRITTEN count.

## Module responsibilities
| Module                     | Responsibility                                        |
|----------------------------|-------------------------------------------------------|
| main.py                    | Wiring, validation, context, paths, engine wiring     |
| src/core/                  | paths, config, seeds, logging (infrastructure)        |
| src/models/                | Data contracts (frozen dataclasses, SCHEMA_VERSION)   |
| src/generators/            | Clean record streams; know nothing about corruption   |
| src/quality/               | Impurity engine: fixed rule chain of stream transforms|
| src/writers/               | Serialization + atomic file lifecycle                 |
| src/metadata/              | Run manifests (v2) and audit info                     |

## Logging
Contract: ADR-0009. Stages: STARTUP, CONFIG, SEED, GENERATION, QUALITY, WRITER, MANIFEST, SUMMARY.

## Planned (do not create early)
Phase 5: window engine, granularity, incremental INSERT/UPDATE/DELETE, CDC-style.
