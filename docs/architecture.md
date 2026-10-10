# Architecture

## Target pipeline (stable shape - do not change casually)
config (YAML) -> orchestration -> generators -> impurity engine (optional) -> writers -> run manifest

## Current state (end of Milestone 2)
config/pipeline.yaml
    | [CONFIG]
    v
src/core/config.py           typed, validated config (ADR-0008)
    | [SEED]
    v
src/core/seed_manager.py     master seed -> child seed per entity (ADR-0006)
    | [GENERATION]
    v
src/generators/customer.py   streams Customer records (ADR-0005)
    | [WRITER]
    v
src/writers/csv_writer.py    dataclass records -> CSV (ADR-0004)
    | [MANIFEST]
    v
src/metadata/manifest.py     data/generated/_manifests/run_<run_id>.json (ADR-0011)
    | [SUMMARY]
    v
console + logs/run_<run_id>.log

## Module responsibilities
| Module                   | Responsibility                                          |
|--------------------------|---------------------------------------------------------|
| main.py                  | Entry point; wires stages; no business logic            |
| src/core/paths.py        | Path resolution anchored at project root (ADR-0010)     |
| src/core/config.py       | YAML -> typed, validated config objects (ADR-0008)      |
| src/core/seed_manager.py | Deterministic child seeds (ADR-0006)                    |
| src/core/logging_setup.py| Logging contract (ADR-0009)                             |
| src/models/              | Data contracts (frozen dataclasses, ADR-0005/0007)      |
| src/generators/          | Record streams; know nothing about files                |
| src/writers/             | Serialization; only place that knows formats            |
| src/metadata/            | Run manifests and audit info                            |

## Data flow rule
Generators never import writers. Writers never import generators.
main.py is the only module that wires them together.

## Logging
Contract: ADR-0009. Active stages: STARTUP, CONFIG, SEED, GENERATION, WRITER, MANIFEST, SUMMARY.

## Planned (do not create early)
src/quality, src/scenarios, src/streaming, src/orchestration
