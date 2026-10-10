# Configuration

Contract: config/pipeline.yaml, loaded by src/core/config.py (ADR-0008).
Relative paths resolve against the project root (ADR-0010).

## Keys

| Key                       | Type | Required          | Rules                                                 |
|---------------------------|------|-------------------|-------------------------------------------------------|
| run.seed                  | int  | yes               | >= 0; master seed (ADR-0006)                          |
| generation.reference_date | date | yes               | YYYY-MM-DD; simulated "today"                         |
| generation.history        | str  | no (default "3y") | Duration: '30d', '4m', '2y', '10y', '1y6m' (ADR-0012) |
| datasets.<name>.records   | int  | yes               | >= 1                                                  |
| output.directory          | str  | yes               | Relative to project root                              |
| output.format             | str  | yes               | Currently only: csv                                   |

## History (ADR-0012)

`generation.history` controls how far back generated history extends. The
window for historical dates is [reference_date - history_days, reference_date].
Accepted forms: '10y', '2y', '4m', '30d', '1y6m', '2y3m10d'.
Units are fixed lengths (y=365d, m=30d) so config + seed stays deterministic.

## Dataset composition rules

- product requires supplier in datasets (FK dependency, ADR-0014).
  Violations raise ConfigError before anything is written.

## Error behavior
Missing or invalid keys raise ConfigError with a specific message; the run
stops before any file is written.

## Known datasets
customer, supplier, product (Milestone 3b adds employee, order, order_item, payment).
