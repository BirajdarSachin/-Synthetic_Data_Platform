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
- Decision: Frozen dataclasses; generators yield records; pandas only inside writers.
- Why: Dependency-free, testable, streaming core. Frozen records make corruption
  a NEW-record operation (dataclasses.replace) instead of mutation (ADR-0021).
- Alternatives: pandas core (rejected); mutable records (rejected: silent corruption of shared objects).
- Impact: All generators, writers, and the impurity engine.

## ADR-0006 - Reproducibility
- Decision: Master seed + SHA-256 child seeds per entity; only random.Random instances; manifest per run.
- Why: Global seeding breaks under reordering/parallelism.
- Alternatives: random.seed() global (rejected).
- Impact: Seed manager; ADR-0021 extends the scheme to {entity}:quality:{rule}.

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
- Impact: Every module. The QUALITY stage tag was added by ADR-0021.

## ADR-0010 - Relative paths resolve against the project root
- Decision: paths.py anchors all relative paths at the project root.
- Why: Identical behavior from any cwd.
- Alternatives: cwd-relative (rejected).
- Impact: Config, writers, scripts, the quality guard (ADR-0021).

## ADR-0011 - Output directory layout
- Decision: data/generated/ holds generated datasets; run manifests live in data/generated/_manifests/.
- Why: Predictable contract; manifests out of data globs.
- Alternatives: Manifests beside data files (rejected).
- Impact: Writers, main.py. Layout clause superseded by ADR-0019; _manifests/ clause in force.

## ADR-0012 - Configurable history span (generation.history)
- Decision: Optional duration string parsed to history_days; default '3y'; manifest records it.
- Why: History depth is a business decision.
- Alternatives: dateutil (rejected: dependency).
- Impact: config.py; all date-generating entities.

## ADR-0013 - Overwrite policy: rebuild-in-place
- Decision: Re-running overwrites dataset files; writer logs "[WRITER] overwriting existing file=...". Old manifests remain historical records.
- Why: Matches lakehouse partition-overwrite behavior; keeps run -> tweak -> run fast.
- Alternatives: Refuse overwrite (tedious); run_id in filenames (breaks ADR-0004).
- Impact: Writer base class. Atomic writes delivered by ADR-0020. The ADR-0021 guard
  extends this policy: corrupted runs must not overwrite the CLEAN baseline.

## ADR-0014 - Generation order and FK-by-parent-id-range
- Decision: Parents generated before children; child generators receive parent counts and pick FKs from 1..parent_count with their own seeded rng; composition rules validated in main.py.
- Why: Sequential PKs make integrity correct by construction, memory-light, scale-friendly.
- Alternatives: Passing parent id lists for id-only FKs (memory-heavy); post-hoc repair (rejected).
- Impact: All child generators; main.py ordering.

## ADR-0015 - Generation context (parent attributes)
- Decision: When a rule needs parent ATTRIBUTES (not just ids), main.py extracts explicit tuples/dicts from generated parent records and passes them to the child generator constructor. Generators never import each other.
- Why: Rules like order_date >= registration_date are impossible from id ranges alone; explicit wiring keeps generators decoupled and testable.
- Alternatives: Generators reading other generators (coupling); global registry object (premature); post-hoc repair (violates correct-by-construction).
- Impact: main.py holds parent lists in memory (Phase 7 will stream). With ADR-0021, context extraction is defined to use CLEAN records only.

## ADR-0016 - Business rules at generation time
- Decision: Rules are encoded in generators, not checked afterwards: order_date >= customer registration_date; every order >= 1 item; item unit_price = product unit_price; quantity 1..5; payment only for completed orders, <= 1 per order, payment_date >= order_date, amount = item total; reviews reference completed purchases, <= 1 per customer/product pair, anchored to the latest purchase, review_date strictly after it and <= reference_date; same-day purchases ineligible. Impossible requests raise ValueError loudly.
- Why: Data valid by construction is the platform's core value; downstream projects practice VALIDATING these rules.
- Alternatives: Separate repair layer.
- Impact: order/order_item/payment/review generators; tests assert each rule. The ADR-0021 impurity engine is the sanctioned tool that BREAKS these rules under config control.

## ADR-0017 - Writer interface, NDJSON format, shared serialization
- Decision: src/writers/base.py defines Writer (ABC). write() is a template method owning the file lifecycle; subclasses implement only _write_records and may override _open_file. Factory get_writer(format) maps format strings to implementations; main.py never imports concrete writers. JSON output is NDJSON. Serialization rules live in serialization.py.
- Why: The second format made the abstraction earn its place; template method prevents lifecycle drift.
- Alternatives: Single JSON array (rejected); pandas.to_json (rejected: ADR-0005); per-writer serialization (rejected); Protocol instead of ABC (equivalent; ABC chosen).
- Impact: config accepts csv|json|parquet. Narrowed by ADR-0018 (typed formats), extended by ADR-0020 (atomicity).

## ADR-0018 - Parquet output: native types, pinned schema
- Decision: Parquet preserves native field values; the Arrow schema is PINNED from the first record's dataclass annotations (int -> int64, str -> string, date -> date32, Decimal -> decimal128(12, 2)); ParquetWriter writes via pa.Table.from_pandas(frame, schema=...) and pq.write_table.
- Why: ADR-0002 requires DECIMAL semantics; pinning prevents inferred precision drift.
- Alternatives: Text serialization (rejected); pyarrow inference (rejected: precision varies with data); per-writer policy (rejected); .buffer reach-through (rejected); type mapping in serialization.py (rejected: pyarrow on the CSV/JSON path).
- Impact: Partially supersedes ADR-0017's text-serialization scope. Pinned columns are nullable: NULL injection (ADR-0021) produces typed nulls, not string placeholders.

## ADR-0019 - Hive-style partitioned output (supersedes the layout clause of ADR-0011)
- Decision: Generated datasets live at data/generated/{entity}/reference_date={YYYY-MM-DD}/{entity}_{date}.{format}. Manifest schema v2: run header gains manifest_version: 2; dataset entries carry "path" (POSIX, relative to the output directory) instead of "file".
- Why: key=value partitions are required for Athena/Glue pruning (Project 1) and are the natural unit for Phase 5 windowed generation. Date stays in the file name (ADR-0004 holds).
- Breaking-change protocol (rule 26): no downstream consumers existed; flat|hive flag rejected (preserves a layout nobody depends on).
- Alternatives: event_date partition key (deferred to Phase 6's own ADR).
- Impact: main.py owns the path builder; writers stay layout-agnostic; old flat files are stale artifacts.

## ADR-0020 - Atomic writes in the Writer lifecycle
- Decision: Writer.write() directs every format's output to <final>.tmp in the SAME directory, then os.replace() into place on success. Any failure removes the temp file. The actionable PermissionError covers temp-open AND final-replace (a locked FINAL file fails at replace).
- Why: Readers and crashed runs never observe partial files; prerequisite for Phase 4/5 overwrites.
- Alternatives: Write-in-place (rejected); cross-volume temp (rejected: os.replace atomic only per volume); third-party helpers (unnecessary).
- Impact: All writers inherit atomicity via the template method.

## ADR-0021 - Impurity engine: position, seeds, scope, guard
- Decision: A new stage src/quality/ sits between generators and writers as a STREAM TRANSFORM: ImpurityEngine.apply(entity, records) -> records. Rules get their own child seeds keyed "{entity}:quality:{rule}" (never a generator's RNG stream). Corruption is LOCAL: main.py applies the engine at the write boundary, after context extraction (ADR-0015) - child entities are always built from clean records. Null injection (5a): per-record Bernoulli draw at quality.null_rate; a selected record gets exactly ONE configured field nulled via dataclasses.replace (frozen records, ADR-0005); identifier fields (*_id) are rejected at config load. A guard raises ConfigError when quality is active and output.directory is the clean baseline (data/generated). The manifest records the applied quality config. New log stage tag: QUALITY.
- Why: Section 14 of the platform charter demands a separate, configurable impurity mechanism; a stream transform keeps ADR-0005 (flat memory), keeps generators clean (single responsibility), and makes clean-vs-dirty a config toggle over the same seed. Local corruption keeps ADR-0016's valid-by-construction relationships intact - realistic sources have intact relationships; dirt is in the records, not the joins. The guard turns the Excel-lock incident lesson into a mechanism: corrupted runs must never destroy the clean baseline they exist to contrast with.
- Alternatives: Corruption inside generators (rejected: violates the separation charter, couples policies); post-write file corruption (rejected: format-specific surgery on parquet/csv); per-dataset quality output directory config key (rejected: duplicates output.directory responsibility - the guard + explicit output.directory achieves the same with one knob); exact-rate sampling of indices (rejected: requires materializing the stream, breaks streaming at scale); nulling multiple fields per record (rejected: artificially correlates fields); nulling identifiers (rejected: schema-breaking, and deliberately invalid FKs are their own 5b rule).
- Impact: config gains optional quality section (absent = byte-identical clean output); manifest v2 runs may carry a "quality" record; downstream Project 5 (validation/quarantine) consumes corrupted runs; 5b adds duplicates + invalid-FK rules as additional rule classes in the same engine; 5c adds schema drift + out-of-range values.
