# Roadmap & Progress Tracker

## PHASE 0 - Foundations
- [x] Architecture review, decisions D1-D8 (ADR-0001..0008)
- [ ] Project skeleton + logging (Milestone 1)   <- current
- [ ] Clean git history, docs in place

## PHASE 1 - First vertical slice
- [ ] YAML config loading (+ CONFIG logging)
- [ ] Seed manager (+ SEED logging)
- [ ] Customer generator (+ GENERATION logging)
- [ ] CSV writer (+ WRITER logging with output paths)
- [ ] Run manifest (+ MANIFEST / SUMMARY logging)
- [ ] Unit + reproducibility tests

## PHASE 2 - Relational domain (product, order, order_item, payment, referential integrity)
## PHASE 3 - Formats & layout (writer interface, JSON, Parquet)
## PHASE 4 - Impurity engine (nulls, duplicates, invalid FKs, schema drift)
## PHASE 5 - Temporal & incremental (daily/monthly, INSERT/UPDATE/DELETE, CDC-style)
## PHASE 6 - Event streams (event_time vs ingestion_time, late/duplicate/out-of-order)
## PHASE 7 - Scale (chunking, multiprocessing)
## PHASE 8 - Production readiness (packaging, CLI, metadata catalog)
