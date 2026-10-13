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
| output.format             | str  | yes               | csv \| json \| parquet (ADR-0017)                          |
| quality                   | map  | no                | Absent = clean runs (ADR-0021)                        |
| quality.null_rate         | num  | with quality      | 0..1; fraction of records corrupted                   |
| quality.datasets.<name>.null_fields | list | with quality | Column names; identifier fields (*_id) rejected |

## Output layout (ADR-0019)
{output.directory}/{entity}/reference_date={YYYY-MM-DD}/{entity}_{date}.{format}.
Manifests: {output.directory}/_manifests/run_<run_id>.json (manifest_version 2;
a quality run records its quality config in the manifest).

## Quality / impurity (ADR-0021)
- The engine corrupts one randomly chosen configured field on a deterministic
  fraction of records (per-record draw, own child seed per entity+rule).
- Corruption is local: child-entity context is always built from clean records.
- Identifier fields (*_id) cannot be nulled; deliberately invalid FKs are a
  separate rule (Milestone 5b).
- GUARD: quality active + output.directory = data/generated raises ConfigError.
  Corrupted runs belong in data/corrupted.

## Dataset composition rules (ADR-0014/0016)
- product requires supplier; order requires customer and employee.
- order_item requires order and product; order_item.records >= order.records.
- payment requires order and order_item. review requires order and order_item.

## Error behavior
Invalid config raises ConfigError before any file is written; rule violations
during generation raise ValueError; all writes are atomic (ADR-0020).

## Known datasets
customer, supplier, product, employee, order, order_item, payment, review.
