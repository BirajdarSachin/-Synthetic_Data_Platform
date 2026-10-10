# Architecture

## Target pipeline (stable shape - do not change casually)
config (YAML) → orchestration → generators → impurity engine (optional) → writers → run manifest

## Current state (end of Milestone 1)
main.py → src/core/logging_setup.py → console (INFO) + logs/run_<run_id>.log (DEBUG)

## Module responsibilities
| Module     | Responsibility                                                  |
|------------|-----------------------------------------------------------------|
| main.py    | Entry point only - no business logic                            |
| src/core/  | Infrastructure everything depends on (now: logging; next: config, seeds) |
| docs/      | Contracts: architecture, decisions (ADRs), roadmap              |

## Logging
Contract: ADR-0009 in docs/decisions.md.

## Planned (do not create early)
src/generators, src/models, src/writers, src/quality, src/metadata, tests/, config/, data/
