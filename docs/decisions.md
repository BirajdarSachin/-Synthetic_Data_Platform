# Decision Log (ADR)

Format: Decision / Why / Alternatives / Impact.
ADRs are immutable. To change a decision, write a new ADR that supersedes it.

## ADR-0001 - Sequential integer primary keys
- Decision: Sequential int PKs (1, 2, 3, ...).
- Why: Deterministic, debuggable, ordered (helps CDC later).
- Alternatives: UUIDs (harder to eyeball; addable later as extra columns).
- Impact: All models; enables ADR-0014.

## ADR-0002 - Money as decimal
- Decision: decimal.Decimal in models; writers serialize with exactly 2 decimal places.
- Why: Floats corrupt money; warehouses expect DECIMAL semantics.
- Alternatives: Integer cents; float (rejected).
- Impact: product.unit_price, order_item.unit_price, payment.amount; serialization.py.

## ADR-0003 - Time conventions
- Decision: UTC ISO-8601 timestamps; plain dates; "now" injected from config, never the real clock.
- Why: Determinism; foundation for late/out-of-order events.
- Alternatives: Real clock (non-reproducible).
- Impact: All timestamped entities; Phases 5-6.

## ADR-0004 - Naming conventions
- Decision: Entities singular snake_case; columns snake_case; files {entity}_{date}.{format}.
- Why: One unambiguous convention.
- Alternatives: Plural names.
- Impact: All datasets, files, future tables.

## ADR-0005 - Internal representation
- Decision: Frozen dataclasses; generators yield records; pandas only inside writers (Phase 3+).
- Why: Dependency-free, testable, streaming core.
- Alternatives: pandas core (rejected).
- Impact: All generators and writers; pandas entered at Milestone 4b inside ParquetWriter only.

## ADR-0006 - Reproducibility
- Decision: Master seed + SHA-256 child seeds per entity; only random.Random instances; manifest per run.
- Why: Global seeding breaks under reordering/parallelism.
- Alternatives: random.seed() global (rejected).
- Impact: Seed manager; future per-window seeds (Phase 5).

## ADR-0007 - Schema versioning
- Decision: Models own SCHEMA_VERSION; additive = minor; removal/type change = major + approval.
- Why: Downstream projects must detect change.
- Alternatives: None (silent drift unacceptable).
- Impact: All models; manifests; Parquet schema pinned from dataclass annotations (ADR-0018).

## ADR-0008 - Configuration format
- Decision: YAML validated into typed frozen dataclasses.
- Why: Readable, comment-friendly, diffable.
- Alternatives: JSON, TOML.
- Impact: config/ + loader.

## ADR-0009 - Logging contract
- Decision: stdlib logging; per-module loggers; console INFO, per-run file DEBUG; "[STAGE] message key=value"; stage tags STARTUP/CONFIG/SEED/GENERATION/WRITER/MANIFEST/SUMMARY; crashes logged via run() wrapper, exit codes 0/1/130.
- Why: Contracts, greppability, forensics.
- Alternatives: print; structlog (deferred).
- Impact: Every module.

## ADR-0010 - Relative paths resolve against the project root
- Decision: paths.py anchors all relative paths at the project root.
- Why: Identical behavior from any cwd.
- Alternatives: cwd-relative (rejected).
- Impact: Config, writers, scripts.

## ADR-0011 - Output directory layout
- Decision: data/generated/ holds generated datasets; run manifests live in data/generated/_manifests/.
- Why: Predictable contract; manifests out of data globs.
- Alternatives: Manifests beside data files (rejected).
- Impact: Writers, main.py. SUPERSEDED in part by ADR-0019 (dataset layout is now Hive-style); the _manifests/ clause remains in force.

## ADR-0012 - Configurable history span (generation.history)
- Decision: Optional duration string parsed to history_days; default '3y'; manifest records it.
- Why: History depth is a business decision.
- Alternatives: dateutil (rejected: dependency).
- Impact: config.py; all date-generating entities.

## ADR-0013 - Overwrite policy: rebuild-in-place
- Decision: Re-running overwrites dataset files; writer logs "[WRITER] overwriting existing file=...". Old manifests remain historical records.
- Why: Matches lakehouse partition-overwrite behavior; keeps run -> tweak -> run fast.
- Alternatives: Refuse overwrite (tedious); run_id in filenames (breaks ADR-0004).
- Impact: Writer base class. Atomic writes delivered by ADR-0020.

## ADR-0014 - Generation order and FK-by-parent-id-range
- Decision: Parents generated before children; child generators receive parent counts and pick FKs from 1..parent_count with their own seeded rng; composition rules validated in main.py.
- Why: Sequential PKs make integrity correct by construction, memory-light, scale-friendly.
- Alternatives: Passing parent id lists for id-only FKs (memory-heavy); post-hoc repair (rejected).
- Impact: All child generators; main.py ordering.

## ADR-0015 - Generation context (parent attributes)
- Decision: When a rule needs parent ATTRIBUTES (not just ids), main.py extracts explicit tuples/dicts from generated parent records and passes them to the child generator constructor. Generators never import each other.
- Why: Rules like order_date >= registration_date are impossible from id ranges alone; explicit wiring keeps generators decoupled and testable.
- Alternatives: Generators reading other generators (coupling); global registry object (premature); post-hoc repair (violates correct-by-construction).
- Impact: main.py holds parent lists in memory (fine at thousands of rows; Phase 7 will stream). Context consumers: order, order_item, payment, review.

## ADR-0016 - Business rules at generation time
- Decision: Rules are encoded in generators, not checked afterwards: order_date >= customer registration_date; every order >= 1 item; item unit_price = product unit_price; quantity 1..5; payment only for completed orders, <= 1 per order, payment_date >= order_date, amount = item total; reviews reference completed purchases, <= 1 per customer/product pair, anchored to the latest purchase, review_date strictly after it and <= reference_date; same-day purchases ineligible. Impossible requests raise ValueError loudly.
- Why: Data valid by construction is the platform's core value; downstream projects practice VALIDATING these rules.
- Alternatives: Separate repair layer (deferred to Phase 4 impurity engine, which breaks these rules under controlled config).
- Impact: order/order_item/payment/review generators; tests assert each rule.

## ADR-0017 - Writer interface, NDJSON format, shared serialization
- Decision: src/writers/base.py defines Writer (ABC). write() is a template method owning the file lifecycle; subclasses implement only _write_records and may override _open_file. Factory get_writer(format) maps format strings to implementations; main.py never imports concrete writers. JSON output is NDJSON (one object per line, UTF-8, ensure_ascii=False). Serialization rules live in serialization.py.
- Why: The second format made the abstraction earn its place; template method prevents lifecycle drift.
- Alternatives: Single JSON array (rejected: no streaming); pandas.to_json (rejected: ADR-0005); per-writer serialization (rejected: drift); Protocol instead of ABC (equivalent; ABC chosen).
- Impact: config accepts csv|json|parquet; future writers implement Writer. Text-serialization scope narrowed by ADR-0018; atomicity added by ADR-0020.

## ADR-0018 - Parquet output: native types, pinned schema
- Decision: Parquet preserves native field values: dates remain dates (Arrow date32[day]) and Decimal money remains decimal. serialization.py owns both policies: serialize_record() for text formats, native_record() for typed formats. The Arrow schema is PINNED, derived from the first record's dataclass annotations (int -> int64, str -> string, date -> date32, Decimal -> decimal128(12, 2)); ParquetWriter opens a binary handle and writes via pa.Table.from_pandas(frame, schema=...) and pq.write_table.
- Why: ADR-0002 requires DECIMAL semantics; native types enable warehouse type checks and predicate pushdown. Pinning is required because pyarrow infers decimal precision from the values present (spike: unit_price 5.00-2000.00 inferred decimal128(6, 2)); a different config would produce a different column type - schema drift, which ADR-0007 exists to prevent.
- Alternatives: Text serialization (rejected: dates/money become strings); pyarrow inference (rejected: inferred precision varies with generation parameters); per-writer conversion policy (rejected: duplicates policy outside serialization.py); text-handle .buffer reach-through (rejected: bypasses the handle contract); type mapping in serialization.py (rejected: puts pyarrow on the CSV/JSON import path).
- Impact: Partially supersedes ADR-0017's statement that text serialization is "used by every writer". The base Writer is generic over text and binary handles. Parquet materializes records in memory; chunked writing deferred to Phase 7.

## ADR-0019 - Hive-style partitioned output (supersedes the layout clause of ADR-0011)
- Decision: Generated datasets live at data/generated/{entity}/reference_date={YYYY-MM-DD}/{entity}_{date}.{format}. The _manifests/ location from ADR-0011 is unchanged. Manifest schema v2: the run header gains manifest_version: 2, and each dataset entry replaces "file" (bare name) with "path" (POSIX-style path relative to the output directory). File names keep the ADR-0004 {entity}_{date}.{format} contract inside the partition directory.
- Why: key=value partition directories are the layout AWS Athena/Glue require for partition pruning (Project 1) and the natural unit for Phase 5 windowed generation (one file per window) and Phase 6 event data. The date stays in the file name so ADR-0004 keeps holding and humans can glob without parsing directories.
- Breaking-change protocol (rule 26): current behavior was flat files; affected consumers: none exist yet (platform is pre-release), so breaking now costs zero versus unbounded later; backward-compatible alternative considered: a flat|hive config flag (rejected - preserves a layout nobody depends on and forces Phase 5 to maintain two path schemes); old flat outputs remain on disk as stale artifacts and may be deleted; old manifests stay historical (ADR-0013 already accepts manifest/file divergence).
- Alternatives: flat|hive flag (rejected above); partition key named event_date (deferred - Phase 6 introduces event time vs ingestion time and will revisit the key name in its own ADR).
- Impact: main.py owns the path builder (layout is orchestration wiring; paths.py stays single-purpose resolution; writers stay layout-agnostic); manifest.py v2; old flat files stale.

## ADR-0020 - Atomic writes in the Writer lifecycle
- Decision: Writer.write() directs every format's output to a temporary file (<final name>.tmp) in the SAME directory as the final file, then moves it into place with os.replace() on success. Any failure - write error, PermissionError, Ctrl+C - removes the temp file first. The actionable PermissionError message now covers both the temp-open and the final-replace step (a locked FINAL file fails at replace, not at open).
- Why: Fulfills the promise recorded in ADR-0013/0017; downstream readers and crashed runs can never observe a partially written file - a prerequisite for Phase 4 (quarantine paths) and Phase 5 (overwriting window partitions).
- Alternatives: Write-in-place (rejected: partial files observable on crash/lock); temp in the system temp directory (rejected: os.replace is only atomic within one volume, so same-directory temp is required); third-party helpers (rejected: stdlib os.replace suffices).
- Impact: All three writers inherit atomicity unchanged via the template method - no writer code changed; tests pin temp cleanup, the locked-target message, and rebuild-in-place behavior.
