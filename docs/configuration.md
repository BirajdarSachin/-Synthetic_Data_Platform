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
| quality.null_rate         | num  | with quality      | 0..1; one field nulled on a fraction of records       |
| quality.duplicate_rate    | num  | no (default 0.0)  | 0..1; records re-emitted as exact delayed copies      |
| quality.invalid_fk_rate   | num  | no (default 0.0)  | 0..1; records with an out-of-range FK                 |
| quality.out_of_range_rate | num  | no (default 0.0)  | 0..1; numeric fields negated (ADR-0023)               |
| quality.drift_rate        | num  | no (default 0.0)  | 0..1; records with dropped/extra columns (ADR-0023)   |
| quality.datasets.<n>.null_fields       | list | no | Non-id columns; identifiers rejected               |
| quality.datasets.<n>.invalid_fk_fields | map  | no | FK field -> parent dataset; only *_id fields       |
| quality.datasets.<n>.out_of_range_fields | list | no | Numeric non-id columns; overlap with null_fields rejected |
| quality.datasets.<n>.drop_fields         | list | no | Non-id columns removed from drifted records        |
| quality.datasets.<n>.extra_fields        | list | no | Marker columns added to drifted records            |

## Output layout (ADR-0019)
{output.directory}/{entity}/reference_date={YYYY-MM-DD}/{entity}_{date}.{format}.
Manifests: {output.directory}/_manifests/run_<run_id>.json (manifest_version 2;
a quality run records its full quality config in the manifest).

## Quality / impurity (ADR-0021/0022/0023)
- Fixed chain: null -> invalid_fk -> out_of_range -> drift -> duplicate (last).
- Each rule has its own child seed: {entity}:quality:{rule}.
- Protection symmetry: null/out_of_range/drop reject *_id fields; invalid_fk
  requires *_id fields.
- Out-of-range strategy: value -> -abs(value); numeric fields only.
- Drift: dropped column and/or extra marker column "__unexpected__" on
  drifted records. Format behavior: CSV dropped->empty cell / extra->loud
  error; JSON natural; Parquet drift is VISIBLE IN THE FILE SCHEMA.
- Corruption is local: child-entity context is always built from clean records.
- GUARD: quality active + output.directory = data/generated raises ConfigError.

## Dataset composition rules (ADR-0014/0016)
- product requires supplier; order requires customer and employee.
- order_item requires order and product; order_item.records >= order.records.
- payment requires order and order_item. review requires order and order_item.

## Error behavior
Invalid config raises ConfigError before any file is written; rule violations
during generation raise ValueError; all writes are atomic (ADR-0020).

## Known datasets
customer, supplier, product, employee, order, order_item, payment, review.
