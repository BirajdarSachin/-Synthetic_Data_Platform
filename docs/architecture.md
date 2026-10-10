# Architecture

## Target pipeline (stable shape - do not change casually)
config (YAML) -> orchestration -> generators -> impurity engine (optional) -> writers -> run manifest

## Current state (end of Milestone 3b)
config/pipeline.yaml
    | [CONFIG]
    v
src/core/config.py           typed, validated config (ADR-0008)
    | [SEED]
    v
src/core/seed_manager.py     master seed -> child seed per entity (ADR-0006)
    | [GENERATION]
    v
generators in FK order (ADR-0014):
  customer, supplier, employee  (independent)
  product                       (supplier count)
  order                         (customer dates + employee count, ADR-0015 context)
  order_item                    (order count + product prices, ADR-0015 context)
  payment                       (completed orders + item totals, ADR-0015 context)
    | [WRITER]
    v
src/writers/csv_writer.py    dataclass records -> CSV (ADR-0004)
    | [MANIFEST]
    v
src/metadata/manifest.py     data/generated/_manifests/run_<run_id>.json (ADR-0011)

## Data flow rules
- Generators never import writers; writers never import generators.
- main.py is the ONLY module that wires stages and builds the generation
  context (parent attributes passed to child generators, ADR-0015).
- Parent id-range FKs need only counts (ADR-0014); business rules that need
  parent attributes receive explicit tuples/dicts from main.py (ADR-0015).

## Module responsibilities
| Module                    | Responsibility                                       |
|---------------------------|------------------------------------------------------|
| main.py                   | Wiring, composition validation, context building     |
| src/core/                 | paths, config, seeds, logging (infrastructure)       |
| src/models/               | Data contracts (frozen dataclasses, SCHEMA_VERSION)  |
| src/generators/           | Record streams; know nothing about files             |
| src/writers/              | Serialization; only place that knows formats         |
| src/metadata/             | Run manifests and audit info                         |

## Logging
Contract: ADR-0009. Stages: STARTUP, CONFIG, SEED, GENERATION, WRITER, MANIFEST, SUMMARY.
Crashes are logged via run() with exit codes 0/1/130.

## Planned (do not create early)
src/quality, src/scenarios, src/streaming, src/orchestration
