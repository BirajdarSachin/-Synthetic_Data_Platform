# Configuration

Contract: config/pipeline.yaml, loaded by src/core/config.py (ADR-0008).
Relative paths resolve against the project root (ADR-0010).

## Keys

| Key                       | Type | Required          | Rules                                                 |
|---------------------------|------|-------------------|-------------------------------------------------------|
| run.seed                  | int  | yes               | >= 0; master seed (ADR-0006)                          |
| generation.reference_date | date | yes               | YYYY-MM-DD; simulated "today"                         |
| generation.history        | str  | no (default "3y") | Duration: '30d', '4m', '2y', '10y', '1y6m' (ADR-0012) |
| generation.window         | map  | no                | Absent = single snapshot per entity (ADR-0024)        |
| generation.window.granularity | str | with window    | daily \| monthly                                       |
| generation.window.mode    | str  | no (default full) | full = all history windows; latest = only reference window |
| datasets.<name>.records   | int  | yes               | >= 1                                                  |
| output.directory          | str  | yes               | Relative to project root                              |
| output.format             | str  | yes               | csv \| json \| parquet (ADR-0017); extension follows format |
| quality                   | map  | no                | Absent = clean runs (ADR-0021)                        |
| quality.null_rate         | num  | with quality      | 0..1                                                  |
| quality.duplicate_rate    | num  | no (default 0.0)  | 0..1                                                  |
| quality.invalid_fk_rate   | num  | no (default 0.0)  | 0..1                                                  |
| quality.out_of_range_rate | num  | no (default 0.0)  | 0..1 (ADR-0023)                                       |
| quality.drift_rate        | num  | no (default 0.0)  | 0..1 (ADR-0023)                                       |
| quality.datasets.<n>.*    | map  | no                | See ADR-0021..0023 (null_fields, invalid_fk_fields, out_of_range_fields, drop_fields, extra_fields) |

## Output layout (ADR-0019 / ADR-0024)
- Snapshot entities (always supplier, product; ALL entities without a window
  config): {output.directory}/{entity}/reference_date={date}/{entity}_{date}.{format}
- Windowed fact entities (customer, employee, order, order_item, payment,
  review; order_item via its order's date):
  {output.directory}/{entity}/event_date={window_key}/{entity}_{window_key}.{format}
- Manifests: {output.directory}/_manifests/run_<run_id>.json (manifest_version 3:
  dataset entries carry layout + files[] with per-file path/records/sha256;
  the header carries the window config).

## Windowing (ADR-0024)
- Window keys derive from each record's business date; daily = "YYYY-MM-DD",
  monthly = "YYYY-MM".
- PK continuity: the union of all window files of an entity is exactly 1..N;
  individual window files contain a deterministic subset (gaps are normal in
  mode="latest" - that is what an incremental delta file looks like).
- Temporal integrity across windows is inherited from ADR-0016 rules.
- Corruption applies per window stream with per-window seed scopes; a nulled
  date never moves a record between windows.

## Quality / impurity (ADR-0021/0022/0023)
- Fixed chain: null -> invalid_fk -> out_of_range -> drift -> duplicate (last).
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
