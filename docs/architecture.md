# Architecture

## Target pipeline (stable shape - do not change casually)
config (YAML) -> orchestration -> generators -> impurity engine (optional) -> writers -> run manifest

## Current state (end of Milestone 5a)
config/pipeline.yaml
    | [CONFIG]
    v
src/core/config.py           typed, validated config (ADR-0008)
    | [SEED]
    v
src/core/seed_manager.py     master seed -> child seeds (entities + quality, ADR-0006/0021)
    | [GENERATION]
    v
generators in FK order (ADR-0014), fed context by main.py (ADR-0015)
    | context extracted from CLEAN records only
    v
src/quality/engine.py        ImpurityEngine (optional; ADR-0021)  [QUALITY]
    | passthrough when no quality config
    v
main.py path builder: {entity}/reference_date={date}/{entity}_{date}.{fmt} (ADR-0019)
    |
src/writers/__init__.py      get_writer(format) factory (ADR-0017)
    |
CsvWriter / JsonWriter / ParquetWriter - all extend Writer ABC; ATOMIC (ADR-0020)
    | [MANIFEST]
    v
src/metadata/manifest.py     manifest v2 + optional quality record (ADR-0011/0019/0021)

## Data flow rules
- Generators never import writers or src/quality; src/quality imports neither.
- Corruption is LOCAL: the engine sits at the write boundary; context for
  child entities is always built from clean records (ADR-0015/0021).
- Quality rules get their own child seeds: {entity}:quality:{rule} (ADR-0021).
- Absent quality config = byte-identical clean output.
- quality runs must target a directory other than the clean baseline (guard).

## Module responsibilities
| Module                     | Responsibility                                        |
|----------------------------|-------------------------------------------------------|
| main.py                    | Wiring, validation, context, paths, engine wiring     |
| src/core/                  | paths, config, seeds, logging (infrastructure)        |
| src/models/                | Data contracts (frozen dataclasses, SCHEMA_VERSION)   |
| src/generators/            | Clean record streams; know nothing about corruption   |
| src/quality/               | Impurity engine: stream transforms (ADR-0021)         |
| src/writers/               | Serialization + atomic file lifecycle                 |
| src/metadata/              | Run manifests (v2) and audit info                     |

## Logging
Contract: ADR-0009. Stages: STARTUP, CONFIG, SEED, GENERATION, QUALITY, WRITER, MANIFEST, SUMMARY.
The QUALITY tag was added by ADR-0021.

## Planned (do not create early)
5b: duplicates + invalid FK rules. 5c: schema drift + out-of-range values.
