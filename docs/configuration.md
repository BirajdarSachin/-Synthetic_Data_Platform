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
Window for historical dates: [reference_date - history_days, reference_date].
Forms: '10y', '2y', '4m', '30d', '1y6m', '2y3m10d'; fixed units (y=365d, m=30d).

## Dataset composition rules (ADR-0014/0016)
- product requires supplier
- order requires customer and employee
- order_item requires order and product; order_item.records >= order.records
- payment requires order and order_item; payment.records must not exceed the
  number of completed orders (~70% of order.records with the default split) -
  otherwise a ValueError stops the run loudly.

## Error behavior
Invalid config raises ConfigError before any file is written; rule violations
during generation raise ValueError and are logged by run() with exit code 1.

## Known datasets
customer, supplier, product, employee, order, order_item, payment.
