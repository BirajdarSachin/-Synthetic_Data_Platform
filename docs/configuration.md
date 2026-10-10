# Configuration

Contract: config/pipeline.yaml, loaded by src/core/config.py (ADR-0008).
Relative paths resolve against the project root (ADR-0010).

## Keys

| Key                       | Type | Required | Rules                            |
|---------------------------|------|----------|----------------------------------|
| run.seed                  | int  | yes      | >= 0; master seed (ADR-0006)     |
| generation.reference_date | date | yes      | YYYY-MM-DD; simulated "today"    |
| datasets.<name>.records   | int  | yes      | >= 1                             |
| output.directory          | str  | yes      | Relative to project root         |
| output.format             | str  | yes      | Currently only: csv              |

## Error behavior
Missing or invalid keys raise ConfigError with a specific message; the run
stops before any file is written.

## Known datasets
customer (Phase 2 adds product, order, order_item, payment).
