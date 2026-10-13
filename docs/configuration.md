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
| output.format             | str  | yes               | csv \| json \| parquet (ADR-0017); extension follows format |
| quality                   | map  | no                | Absent = clean runs (ADR-0021)                        |
| quality.null_rate         | num  | with quality      | 0..1; fraction of records getting one field nulled    |
| quality.duplicate_rate    | num  | no (default 0.0)  | 0..1; fraction of records re-emitted as exact copies  |
| quality.invalid_fk_rate   | num  | no (default 0.0)  | 0..1; fraction of records with an out-of-range FK     |
| quality.datasets.<n>.null_fields       | list | no | Non-id columns; identifier fields (*_id) rejected   |
| quality.datasets.<n>.invalid_fk_fields | map  | no | FK field -> parent dataset name; only *_id fields   |

## Output layout (ADR-0019)
{output.directory}/{entity}/reference_date={YYYY-MM-DD}/{entity}_{date}.{format}.
Manifests: {output.directory}/_manifests/run_<run_id>.json (manifest_version 2;
a quality run records its full quality config in the manifest).

## Quality / impurity (ADR-0021/0022)
- Fixed rule chain per entity: null -> invalid_fk -> duplicate. Duplication
  runs last, so duplicates are exact copies of already-corrupted records.
- Each rule has its own child seed: {entity}:quality:{rule}.
- Null rule: one configured field per selected record; identifier fields
  (*_id) rejected. Invalid-FK rule: only *_id fields; the invalid value is
  out-of-range (> parent record count) because parent PKs are sequential
  1..N (ADR-0001); the field -> parent mapping is explicit in config.
- Duplicate rule: selected records re-emitted after a delay of 1..max_delay
  originals (bounded buffer, O(max_delay) memory).
- Corruption is local: child-entity context is always built from clean records.
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
