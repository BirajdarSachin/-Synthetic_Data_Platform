# Decision Log (ADR)

Format: Decision / Why / Alternatives / Impact.
ADRs are immutable. To change a decision, write a new ADR that supersedes it.

## ADR-0001 - Sequential integer primary keys
- Decision: Sequential int PKs (1, 2, 3, ...).
- Why: Deterministic, debuggable, ordered (helps CDC later).
- Alternatives: UUIDs (harder to eyeball; addable later as extra columns).
- Impact: All models; enables ADR-0014; enables out-of-range invalid FKs (ADR-0022).

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
- Alternatives: pandas core (rejected); mutable records (rejected).
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
- Decision: stdlib logging; per-module loggers; console INFO, per-run file DEBUG; "[STAGE] message key=value"; stage tags STARTUP/CONFIG/SEED/GENERATION/QUALITY/WRITER/MANIFEST/SUMMARY; crashes logged via run() wrapper, exit codes 0/1/130.
- Why: Contracts, greppability, forensics.
- Alternatives: print; structlog (deferred).
- Impact: Every module. QUALITY tag added by ADR-0021.

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
- Impact: Writer base class. Atomic writes delivered by ADR-0020. The ADR-0021 guard extends this policy: corrupted runs must not overwrite the CLEAN baseline.

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
- Impact: order/order_item/payment/review generators; tests assert each rule. The impurity engine (ADR-0021/0022) is the sanctioned tool that BREAKS these rules under config control.

## ADR-0017 - Writer interface, NDJSON format, shared serialization
- Decision: src/writers/base.py defines Writer (ABC). write() is a template method owning the file lifecycle; subclasses implement only _write_records and may override _open_file. Factory get_writer(format) maps format strings to implementations; main.py never imports concrete writers. JSON output is NDJSON. Serialization rules live in serialization.py.
- Why: The second format made the abstraction earn its place; template method prevents lifecycle drift.
- Alternatives: Single JSON array (rejected); pandas.to_json (rejected: ADR-0005); per-writer serialization (rejected); Protocol instead of ABC (equivalent; ABC chosen).
- Impact: config accepts csv|json|parquet. Narrowed by ADR-0018, extended by ADR-0020.

## ADR-0018 - Parquet output: native types, pinned schema
- Decision: Parquet preserves native field values; the Arrow schema is PINNED from the first record's dataclass annotations (int -> int64, str -> string, date -> date32, Decimal -> decimal128(12, 2)); ParquetWriter writes via pa.Table.from_pandas(frame, schema=...) and pq.write_table.
- Why: ADR-0002 requires DECIMAL semantics; pinning prevents inferred precision drift.
- Alternatives: Text serialization (rejected); pyarrow inference (rejected); per-writer policy (rejected); .buffer reach-through (rejected); type mapping in serialization.py (rejected).
- Impact: Partially supersedes ADR-0017's text-serialization scope. Pinned columns are nullable: NULL injection produces typed nulls.

## ADR-0019 - Hive-style partitioned output (supersedes the layout clause of ADR-0011)
- Decision: Generated datasets live at data/generated/{entity}/reference_date={YYYY-MM-DD}/{entity}_{date}.{format}. Manifest schema v2: run header gains manifest_version: 2; dataset entries carry "path" (POSIX, relative to the output directory) instead of "file".
- Why: key=value partitions are required for Athena/Glue pruning (Project 1) and are the natural unit for Phase 5 windowed generation. Date stays in the file name (ADR-0004 holds).
- Breaking-change protocol (rule 26): no downstream consumers existed; flat|hive flag rejected.
- Alternatives: event_date partition key (deferred to Phase 6's own ADR).
- Impact: main.py owns the path builder; writers stay layout-agnostic; old flat files are stale artifacts.

## ADR-0020 - Atomic writes in the Writer lifecycle
- Decision: Writer.write() directs every format's output to <final>.tmp in the SAME directory, then os.replace() into place on success. Any failure removes the temp file. The actionable PermissionError covers temp-open AND final-replace.
- Why: Readers and crashed runs never observe partial files; prerequisite for Phase 4/5 overwrites.
- Alternatives: Write-in-place (rejected); cross-volume temp (rejected); third-party helpers (unnecessary).
- Impact: All writers inherit atomicity via the template method.

## ADR-0021 - Impurity engine: position, seeds, scope, guard
- Decision: A new stage src/quality/ sits between generators and writers as a STREAM TRANSFORM: ImpurityEngine.apply(entity, records) -> records. Rules get their own child seeds keyed "{entity}:quality:{rule}". Corruption is LOCAL: main.py applies the engine at the write boundary, after context extraction (ADR-0015). Null injection: per-record Bernoulli draw; a selected record gets exactly ONE configured field nulled via dataclasses.replace; identifier fields (*_id) rejected at config load. A guard raises ConfigError when quality is active and output.directory is the clean baseline. The manifest records the applied quality config. New log stage tag: QUALITY.
- Why: Charter section 14 demands a separate, configurable impurity mechanism; a stream transform keeps ADR-0005 and keeps generators clean; local corruption keeps ADR-0016 relationships intact; the guard turns the Excel-lock lesson into a mechanism.
- Alternatives: Corruption inside generators (rejected); post-write file corruption (rejected); per-dataset output config key (rejected); exact-rate sampling (rejected: materializes the stream); nulling multiple fields (rejected); nulling identifiers (rejected).
- Impact: config gains optional quality section (absent = byte-identical clean output); manifest carries a quality record; 5b adds rule classes to the same engine; 5c adds schema drift + out-of-range values.

## ADR-0022 - Duplicate and invalid-FK rules; fixed rule chain
- Decision: Two new rule classes join the engine. Chain order is FIXED: null -> invalid_fk -> duplicate; duplication runs LAST so duplicates are exact copies of already-corrupted records. DuplicateRule re-emits a selected record after a random delay of 1..max_delay ORIGINAL records using a bounded position-keyed buffer (O(max_delay) memory). InvalidForeignKeyRule replaces a *_id field with an out-of-range id (parent_count + rng(1..1000)) - valid because parent PKs are exactly 1..N (ADR-0001). Config: quality.duplicate_rate and quality.invalid_fk_rate (default 0.0, backward compatible); invalid_fk_fields is an EXPLICIT field -> parent dataset mapping; the null rule rejects *_id while the invalid-FK rule requires *_id (protection symmetry). The engine takes dataset_counts (optional parameter; loud ValueError if an invalid-FK rule needs them without counts); main.py validates that every referenced parent exists in datasets. New rates are recorded in the manifest quality record.
- Why: Duplicates and broken FKs are the two most common real-world source problems and the first deliberate ADR-0016 violations. Delayed re-emission mirrors how real systems duplicate (upstream retries), unlike adjacent doubling. Out-of-range ids make 'invalid' provable without parent lookups at any scale. Explicit field->parent mapping is required because convention fails (sales_rep_employee_id -> employee).
- Alternatives: Adjacent duplication (rejected: trivially detectable, unrealistic); duplication first in the chain (rejected: copy would be corrupted independently, no longer a duplicate); materializing the stream to sample indices (rejected: breaks streaming, ADR-0005); random existing id as the invalid value (rejected: might be valid); parent-by-naming-convention (rejected: sales_rep_employee_id breaks it); invalid values from a config list (rejected: duplicates the count knowledge the platform already has).
- Impact: Written count can exceed generated count when duplicates are active (manifest records the WRITTEN count; [GENERATION] and [WRITER] counts may differ - expected). Downstream Project 5 can now practice duplicate detection and orphan-FK quarantine. 5c (schema drift, out-of-range values) remains.

## ADR-0023 - Out-of-range values and schema drift; Phase 4 complete
- Decision: Two final rule classes join the engine. OutOfRangeRule negates aconfigured numeric field (-abs(value)) on a selected fraction of records;numeric-only (int/float/Decimal, bool excluded), identifier fields rejected,overlap with null_fields rejected at config load (a nulled value cannot benegated). SchemaDriftRule changes the record SHAPE: on the first corruptedrecord it builds one frozen dataclass via dataclasses.make_dataclass (modelfields minus drop_fields, plus extra_fields with the marker "unexpected")and caches it; drift_rate controls the fraction. Chain is now null ->invalid_fk -> out_of_range -> drift -> duplicate (duplicate still last).
- Why: negative amounts are the classic real-world invalid row; negation isdeterministic and type-preserving so the pinned Parquet schema survives.Schema drift requires a different record shape, and make_dataclass createsit once per rule (never per record), keeping ADR-0005 (frozen records).Format behavior is deliberately heterogeneous and documented: CSV droppedfield -> empty cell, extra field -> loud error; JSON renders naturally;Parquet drift is VISIBLE IN THE FILE SCHEMA (pinned from the first record'sclass) - reading the schema is the drift detector.
Alternatives: value * multiplier (rejected: overflow risks, less canonical);config-supplied invalid values (rejected: duplicates knowledge the platformhas); post-write file surgery for drift (rejected: violates ADR-0021'sstream-transform position); per-record make_dataclass (rejected: wasteful).
- Impact: platform version 1.0.0 - Phase 4 complete. Mixed clean/driftedstreams in Parquet depend on pandas column-union behavior (missing columnsbecome nulls); hardening deferred to Phase 8. Downstream Project 5 can nowpractice detecting all five impurity classes. Phase 5 (windows, incremental,CDC) opens next

## ADR-0024 - Window engine: bucketed partitioned generation
- Decision: generation.window (optional; absent = single snapshot per entity,pre-6a behavior byte-identical) with granularity daily ("YYYY-MM-DD") |monthly ("YYYY-MM") and mode full | latest. Fact entities (customer,employee, order, payment, review by their own business date; order_item viaits order's date) are BUCKETED into event_date= window files at the writeboundary: each entity is still generated as ONE deterministic stream, thenpartitioned. Dimension entities (supplier, product) remain snapshots(reference_date= partition) even in windowed mode. mode="latest" keeps onlythe reference window (the incremental delta-file shape); mode="full" treatsan out-of-range date as a generation bug. Manifest v3: dataset entries gain"layout" and a "files" array (per-file path/records/bytes/sha256); the runheader carries the window config; manifest_version = 3. Impurity enginegains an optional scope: per-window seed keys"{entity}:{window}:quality:{rule}"; corruption applies per window streamAFTER clean bucketing (a nulled date never moves a record between windows).
- Why: Bucketing over per-window independent generation keeps PKs globallysequential for free (union of window files is exactly 1..N - ADR-0001) andinherits cross-window temporal integrity from ADR-0016 rules(order_date >= registration_date implies the order's window >= thecustomer's registration window; same for payment/review), with zero newbusiness rules. The fact/dimension split mirrors real lakehouses:dimensions are snapshots, facts are event-date partitioned - which is thelayout Athena partition pruning requires (Project 1). Individual windowfiles contain a deterministic SUBSET of PKs (gaps are normal, exactly likereal incremental delta files). Per-window independent generation (seed perwindow without regenerating siblings) is deferred to Phase 7.
- Alternatives: per-window independent generation with id offsets (rejectedfor now: complex offset math and per-window customer pools; revisited inPhase 7); one partition key name for both modes (rejected: a snapshot'skey IS the reference date, a fact window's key IS the event window - twosemantics, two honest names); keeping manifest v2 with one path perdataset (rejected: windowed entities have many files; versioned schemachange is the sanctioned mechanism - ADR-0007 thinking applied tometadata).
- Impact: platform 1.1.0. Config gains optional generation.window; manifestreaders must handle v3 (versioned detection); _manifests/ locationunchanged (ADR-0011 clause still in force). Hourly/weekly granularity andper-window independent generation deferred (Phase 7); event_time vsingestion_time and late-arriving data remain Phase 6.
# ADR text for docs/decisions.md (appended by bootstrap_milestone7a.py)

---

## ADR-025 — DQ harness is an output-tree consumer with a fixed 4-check suite

**Status:** Accepted (Milestone 7a)

**Context.** The platform now produces two output trees: the clean baseline
(`data/generated`) and corrupted copies (`data/quality`, impurity engine,
guides in `docs/impurity_guide.md`). Downstream Data Engineering projects need
a repeatable way to prove two things: the baseline honors the platform's
structural guarantees, and the corrupted tree's dirt is *detectable*. That is
the acceptance drill from the operator guide (§11), currently run by hand.

**Decision.**
1. The DQ harness is a **consumer of output files only**. It reads CSV files
   and a DQ config (`configs/dq_base.yaml`); it never imports generator,
   writer, or impurity internals. It is shaped exactly like a downstream ETL
   test suite, so it doubles as a template for future consumers.
2. The 7a suite is fixed and mirrors the impurity chain:
   `not_null -> fk_integrity -> value_in_range -> pk_unique`
   (one check per corruption condition; duplicates and cross-window PK union
   are deferred to 7b; Parquet support to 7b as well).
3. **Two expectation modes** at the entry point `dq_main.py`:
   `--mode clean` (ok iff zero violations) and `--mode corrupt` (ok iff at
   least one check fires). Exit contract unchanged: 0 ok, 1 failure, 130 Ctrl+C.
4. **New log tag `[DQ]`** joins the ADR-0009 tag set. Same `run()`-wrapper
   structure, per-run console logging, atomic JSON report
   (`data/dq/report.json` by default, `.tmp` + `os.replace`).
5. Config keys default to *enabled* when a check toggle is absent; this is a
   documented default (like `quality.output.root`), not hidden behaviour.
   Unknown check names, unknown range keys, `min > max`, all-checks-disabled,
   missing `primary_key`, missing tree/entity directories, and FK parents
   without a `dq.entities` entry are all startup failures (exit 1).
6. CSV null == empty string. Value-based checks skip empty cells; emptiness
   is exclusively `not_null`'s concern — mirroring the impurity engine's Law 2
   so the two sides of the platform reason about nulls identically.

**Consequences.**
- No coupling: DQ can evolve (and be copied into downstream projects) without
  touching the generator core. Tests are pure-stdlib fixtures.
- The check suite is a *contract*: adding a check type or changing semantics
  changes reports and exit behaviour — future additions follow this ADR as a
  new numbered ADR (instance vs new pattern test applies).
- `[DQ]` lines are additive to the ADR-0009 log contract; existing tags and
  exit codes are unchanged, so no §26 protocol is triggered.
- Corrupted trees are *expected* to fail `--mode clean`; failing there is the
  success signal. Tooling that consumes this harness must pass the mode
  explicitly rather than assume one.
# ADR text for docs/decisions.md (appended by bootstrap_milestone7b.py)

---

## ADR-026 — DQ harness 7b: full-row duplicates, row_count bounds, cross-window pk_union, per-entity check gates

**Status:** Accepted (Milestone 7b)

**Context.** Milestone 7a delivered the DQ harness as an output-tree consumer
(ADR-025) with a fixed 4-check suite, and the acceptance drill went green on
the real machine: the clean tree passed with zero violations and the
corrupted tree fired `pk_unique` (356 times) and `fk_integrity` (19 times).
The drill also showed what was left on the table: the duplicate rule's row
growth was only visible through its PK shadow, no check guarded file sizes,
nothing could prove key uniqueness across snapshot windows, and there was no
way to switch a check off for a single entity. Parquet support was deferred
when 7a shipped.

**Decision.**
1. **`duplicate_rows`** joins the suite (default on). Per file, a row that is
   an exact copy of an earlier row (ALL columns, order-stable comparison,
   None normalised to empty string) is a violation; each extra copy counts
   once, matching `pk_unique` semantics. It complements rather than replaces
   `pk_unique`: key-uniqueness and record-copy-ness are different questions
   a downstream consumer is entitled to ask separately. On the impurity
   engine's output both fire, because the duplicate rule appends exact
   copies (interaction Law 4).
2. **`row_count`** joins the suite (default on, but only runs where an
   entity configures `row_count:`). Config accepts `expected: N` (exact) or
   inclusive `min`/`max` bounds; mixing `expected` with bounds is a startup
   error, as are unknown keys, non-integers, negatives, and `min > max`.
   The expectation is PER FILE (per snapshot window). A violation is
   file-level: `row_number=0`, one violation regardless of file size.
   `customer` ships wired to `expected: 1000` in `dq_base.yaml` v4 — proven
   by the accepted drill (clean 1000, corrupted 1023).
3. **`pk_union`** joins the suite as an entity-level, OPT-IN check
   (`pk_union: true` on the entity, plus the global toggle). A primary key
   appearing in MORE THAN ONE file of the entity is a violation listing the
   files; empty keys are skipped (Law 2: emptiness is `not_null`/`pk_unique`'
   business). Opt-in because SCD-style snapshot windows legitimately repeat
   keys across `reference_date` windows. Needs >= 2 files; otherwise the
   runner logs a skip line. The result is entity-level with
   `file='(cross-file union)'`; the runner drives it because its state spans
   files — this is the documented exception to "a Check consumes one file".
4. **Per-entity check gates.** An entity MAY carry `checks: [name, ...]` —
   a whitelist intersected with the global toggles and the config-presence
   rule. A check runs iff (global toggle on) AND (entity config provides
   what it needs) AND (whitelist absent or contains it). Unknown whitelist
   names and an empty whitelist are startup errors with the known-checks
   list printed.
5. **Report accounting decoupled from check execution.** `files_checked`
   and `rows_checked` are counted from the files actually read, no longer
   derived from which checks produced results — gating checks off per entity
   can no longer change what the report says about the tree's size.
   `REPORT_VERSION` stays 1: the JSON shape is unchanged.
6. **Parquet input is deferred to 7c.** Reading Parquet requires a reader
   dependency (pyarrow/pandas); the harness stays stdlib + PyYAML until that
   decision has its own ADR. The CSV-only consumer contract is unchanged.

**Consequences.**
- Existing 7a configs gain `duplicate_rows` behaviour automatically
  (default-on, no config key needed). This is a documented behaviour change:
  corrupted trees now report MORE violations. On the accepted drill the
  corrupt run goes from 375 to ~732 (356 full-row duplicate copies + 1
  customer `row_count` bound); the clean run stays at exactly 0. Machines
  that need the old shape set `dq.checks.duplicate_rows.enabled: false`.
- The `CHECK_NAMES` tuple grows to seven and remains the config contract:
  unknown names in `dq.checks` or in an entity whitelist fail at startup,
  listing the known checks.
- `row_count: {expected: N}` couples the config to generated volumes; the
  yaml comments instruct wiring counts from the clean run's
  `data/dq/report.json` (`rows_checked` per file), and `min`/`max` exist for
  tolerant setups.
- `pk_union`'s entity-level result introduces a synthetic file label
  `(cross-file union)` in reports and logs; per-file row totals are unaffected.
- Adding an 8th check type (or Parquet in 7c) follows this ADR pattern:
  new numbered ADR, config-driven opt-in/opt-out, fail-fast validation.
---

## ADR-027 — DQ harness 7c: expectation wiring from accepted drill evidence

**Status:** Accepted (Milestone 7c — config-only; no harness code changes)

**Context.** The 7b drill went green on the real machine (103 tests; clean
tree 0 violations / 23020 rows; corrupted tree 732 violations / 23376 rows,
exit 0 both). That run closed the code backlog but left three checks without
live proof on real dirt (`not_null` and `value_in_range` never fired; only
`customer` had a `row_count` expectation), and the drill evidence itself
carried the missing facts: the `duplicate_rows` sample lines print full rows,
which leaked the real headers of `order` (`customer_id`, `order_date`,
`order_id`, `sales_rep_employee_id`, `status`) and `order_item` (`order_id`,
`order_item_id`, `product_id`, `quantity`, `unit_price`); the accepted clean
run pinned the per-file row counts; the impurity guide (§6.3) pins
`quantity`'s declared domain at 1..100. The one remaining unknown — WHICH
`order` column the engine's null rule blanks (57 rows) — is resolvable with a
one-line census command before any drill runs.

**Decision.**
1. **`row_count` is wired for all six entities** (`expected:` 1000 / 5000 /
   12000 / 20 / 2000 / 3000, taken verbatim from the accepted clean drill's
   `rows_checked`). Row counts become a contract: any regenerated volume
   change fails loudly, and the fix is re-wiring from `data/dq/report.json`.
   This is the coupling ADR-026's consequences section anticipated; it is now
   deliberate.
2. **`order_item.ranges.quantity = {min: 1, max: 100, type: int}`** mirrors
   the engine's declared domain for `quantity` (impurity guide §6.3), so the
   65 `out_of_range` rows on `data/corrupted` become visible to
   `value_in_range` — its first live proof.
3. **`order.required = [order_id, status]` is PROVISIONAL.** `status` is the
   most likely null-rule target among the five real `order` columns (the
   guide's null rationale is "optional string field at the source";
   `customer_id` is taken by `invalid_fk`, `order_id` is the key). The
   confirmation protocol is a census one-liner counting empty cells per
   column on the corrupted `order` file; if it names a different column, the
   fix is one line in this config. A wrong guess cannot break either drill:
   clean data is complete by construction, and the corrupt contract only
   needs >= 1 fired check.
4. **`order.foreign_keys += sales_rep_employee_id -> employee`.** Second real
   FK, column proven by drill evidence (`sales_rep_employee_id=10`); the
   parent `employee` is already configured with a header-validated
   `employee_id`. Empty FK cells stay `fk_integrity`-skipped (Law 2), so the
   check is safe even if the generator leaves some sales reps unset.
5. **`order_item.product_id -> product` stays UNWIRED.** `product` has no
   `dq.entities` entry (the log's `datasets=8` vs 6 configured entities), and
   the runner fail-fasts on FK parents it cannot load (ADR-025 header
   discipline). Wire the missing entities first, then this FK.
6. **No new check, no code change, `REPORT_VERSION` unchanged.** ADR-026
   pointed Parquet input at 7c; this wiring milestone consumed the 7c slot,
   so Parquet slides to 7d and still needs its reader-dependency ADR.

**Consequences.**
- The clean drill must stay at exactly 0 violations / 23020 rows: every wired
  expectation mirrors generator output by construction, so any clean-tree
  failure now indicates a real change in the generator, not harness noise.
- The corrupted drill is expected to rise from 732 to ~856 violations:
  +57 `not_null` (if `status` is the null column), +65 `value_in_range`,
  +2 `row_count` (order 5110 != 5000, order_item 12223 != 12000). If the
  census names a different null column, the floor is 799 until the one-line
  swap lands. The corrupt contract (ok iff >= 1 check fired) holds either
  way. Counts may drift a few above these numbers if the engine's duplicate
  rule copied already-corrupted rows (copies of dirty rows are equally dirty
  and each counts once).
- All four 7a checks now have live proof on real dirt after the next corrupt
  run, completing the harness's evidence chain against the impurity suite.
- Adding the two unseen entities (and `product`) follows the same shape:
  primary_key + required + row_count from the report, then FK wiring.
---

## ADR-028 — DQ harness 7d: Parquet input — reader dependency and row normalization

**Status:** Accepted (Milestone 7d — ADR-only; zero code, config, or test changes)

**Context.** The harness is an output-tree consumer (ADR-025): it reads
whatever `--tree` points at, plus `configs/dq_base.yaml`. Every tree it has
consumed so far is CSV because the engine writes CSV — but that is the
writer's current choice, not the consumer's contract. Downstream pipelines
exchange data as Parquet far more often than as CSV (columnar, typed,
compressed; the de-facto interchange format of analytics storage), and a
consumer template that can only read CSV models the rare case. The charter
forbids new dependencies without an ADR; ADR-026 deferred Parquet behind
exactly such an ADR, and ADR-027 re-slotted it here after 7c consumed the
wiring milestone. The real design questions are therefore: which reader
dependency, how typed Parquet cells meet a deliberately stringly-typed check
pipeline, and what stays out of scope.

**Decision.**
1. **Format flexibility is a reader concern, not a writer one.** The harness
   accepts MIXED trees: `.csv` and `.parquet` side by side under the same
   entity directories, the per-file format selected by extension alone,
   using the same stem-discovery rules as today (ADR-025 v2.1
   partition-first fallback, prefix-collision guard included). The engine
   and its output layout are untouched; generator-side Parquet output is a
   non-goal of this ADR.
2. **One new dependency: pyarrow, lazy-imported.** `pyarrow.parquet` is
   imported only when a discovered file actually needs reading. CSV-only
   runs never import it: behavior, startup, and bytes are unchanged. If a
   Parquet file is discovered and the import fails, the run fail-fasts with
   a one-line diagnostic (`pyarrow is required for Parquet input: pip
   install pyarrow`) — loud, never silent. Documented floor: pyarrow >= 14;
   no version pin in code.
3. **Reader-boundary normalization is the load-bearing decision.** Every
   Parquet cell is converted to `str` and every null to `""` before any
   check sees it: int -> str(int); float -> repr(float) (shortest
   round-trip form, deterministic since Python 3.1); bool -> "true"/"false";
   date/timestamp -> ISO-8601 string; decimal -> str(decimal). Binary,
   list, struct, and map columns fail fast with a diagnostic (out of
   scope). This keeps checks.py, the runner's accounting, the report JSON
   (`REPORT_VERSION` stays 1), and all 103 tests format-blind. None is
   indistinguishable from a CSV empty cell by design, which preserves Law 2
   (empty FK cells skipped) and not_null's blank semantics exactly; a full
   row survives as the same string tuple the CSV path produces, so
   `duplicate_rows` and the pk checks compare identically across formats.
4. **Header validation parity.** `_validate_header()` validates against the
   Parquet schema's field names exactly as it does against CSV headers (no
   BOM concept exists); a mismatch fail-fasts printing the actual schema
   names, same message shape as the CSV path. Parquet's real types remove
   the encoding class of bugs the CSV path carries (UTF-8-sig, quoting).
5. **No config schema change — now or in the implementation milestone.**
   File extension is the single format selector; a `format:` override key
   is rejected as speculative config. dq_base.yaml keeps saying WHAT to
   check, never HOW files are parsed.
6. **The implementation milestone (7e) is pre-registered here.** Scope: the
   runner's read-path extension; Parquet unit tests built with pyarrow
   inside the test suite under `pytest.importorskip("pyarrow")` so
   dependency-free environments still run the 103; e2e CHECK: a
   mixed-format tree passes the clean drill at 0 violations, and a
   corruption planted in the Parquet entity fires the same check names.
   Nothing in 7d changes behavior, so no drills accompany this milestone.

**Alternatives considered.**
- pandas (`pd.read_parquet`): drags numpy plus transitive dependencies in
  for one function; a second data abstraction the harness does not need.
  Rejected.
- polars: same class of concern; less ubiquitous here than pyarrow, which
  is also the canonical library if the engine ever writes Parquet.
  Rejected.
- duckdb with checks as SQL: a paradigm shift that touches every check and
  the report at once. Rejected as scope creep.
- Stay CSV-only: leaves the consumer template blind to the most common
  downstream interchange format. Rejected (contradicts ADR-025's purpose).
- Wait until the writer emits Parquet: couples the consumer's contract to
  the writer's current serialization choice — exactly backwards. Rejected.

**Consequences.**
- One optional dependency, absent until a Parquet file demands it; CSV
  deployments never notice it exists.
- The conversion table in decision 3 is now contract: 7e test-pins it
  (ints exact; floats round-trip via repr deterministically across
  platforms; nulls indistinguishable from CSV empties by design).
- Violation samples from Parquet files print normalized strings (e.g.,
  quantity -3 renders as "-3"); report shape and the exit-code contract
  are unchanged.
- Manifest v3 sha256 stays format-blind (hashes bytes); the report's
  rows_checked counts Parquet files exactly like CSV files.
- Rollback is trivial: stop pointing trees at Parquet. The CSV path is
  untouched except the shared discovery regex.
- datasets=8 (two unseen entities), `order_item.product_id -> product`,
  and the engine-vs-harness FK reconciliation remain separate backlog
  items, unaffected by this ADR.
---
## ADR-029 — Milestone 6b: incremental change stream + CDC-style change log

**Status:** Accepted (Milestone 6b — new `src/core/cdc/` module, no existing files modified)

**Context.** Phase 5's window engine (ADR-0024) is live: every run writes
per-window partitions (`{tree}/{entity}/reference_date={window}/…`, daily or
monthly keys), fact/dimension split, manifest v3. What the platform does NOT
yet produce is the downstream-facing artifact those windows imply: a change
stream saying WHICH rows appeared, mutated, or vanished between consecutive
windows, in a format a consumer can replay. The roadmap names both halves of
Milestone 6b: an incremental change stream with INSERT/UPDATE/DELETE
semantics, and a CDC-style change log format. The delivery constraint is the
same one ADR-025 faced: the harness-side modules must integrate through
documented, stable contracts (output-tree layouts + config files) because
they are verified and installed without access to the generator's internals.

**Decision.**
1. **CDC is diff-based, over output trees.** `src/core/cdc/` reads TWO
   windows of one entity from the same tree (the same class of reads the DQ
   harness has done since ADR-025) and computes the change stream by
   primary-key diff: key in current window only -> INSERT; key in previous
   window only -> DELETE; key in both with differing row -> UPDATE; key in
   both with identical row -> no event. Zero imports from generator, writer,
   or impurity internals. In-generator event emission (hooks inside the
   pipeline, true transaction ordering) is explicitly OUT OF SCOPE and stays
   a registered future option — it requires touching `main.py`'s core and
   the writers, which this delivery model cannot verify. Diff-based CDC is
   replay-safe, works against every tree the engine has ever written, and
   doubles as the reference implementation a consumer would write.
2. **Change log format: JSONL, full before/after images.** One JSON object
   per line, UTF-8, LF endings:
   `{"seq": 1, "op": "INSERT", "entity": "order", "pk": "o-1",
     "window": "2026-01-15", "prev_window": "2026-01-14",
     "changed": [], "before": null, "after": {...}}`.
   Canonical op names INSERT/UPDATE/DELETE. Full row images (not field
   deltas) keep events self-contained and replayable; `changed` additionally
   carries the sorted list of differing columns for UPDATEs. `before` is
   carried for UPDATE/DELETE and dropped when `options.before_images: false`
   (images always reflect the raw string cells exactly as the files hold
   them — same reader-boundary philosophy as ADR-028 decision 3: every cell
   is a str, missing cells are ""). `seq` is 1-based per entity per run,
   assigned in ascending primary-key order — byte-identical output on
   re-runs, deterministic across platforms.
3. **Window pairing: explicit or auto-detected; first run is the initial
   load.** `cdc_main.py --window W` pairs W with the latest EARLIER window
   present for that entity (auto), unless `--prev-window P` is given
   (explicit; P == W and P > W fail fast). When no earlier window exists,
   the run is an INITIAL LOAD: every current row becomes an INSERT event and
   `prev_window` is null — the standard CDC bootstrap semantics — unless
   `options.initial_load: error` is set, which makes a missing predecessor a
   hard failure for teams that only ever want deltas.
4. **Snapshot validity is enforced.** CDC compares per-window SNAPSHOTS, so
   a primary key may appear at most once per window per entity: a duplicate
   fails fast naming the key and both row numbers. Headers of the two
   compared windows must have the same column set unless
   `options.allow_schema_drift: true`, in which case the diff runs over the
   union of headers and cells absent from a window read as "" (the
   impurity engine's drift rule can add columns, so corrupted trees need
   this switch; clean trees never will).
5. **Output layout, guard, report.** Change logs land in their own derived
   tree, never inside the source: `data/cdc/{entity}/reference_date={window}/
   {prev|initial}__{window}.jsonl` plus one run report `data/cdc/report.json`
   (REPORT_VERSION 1, atomic write, same .tmp + os.replace discipline as
   every writer). The runner refuses any `output_root` that sits inside the
   source tree or vice versa (both directions), mirroring the impurity
   engine's quality-write guard. Per-window filenames carry BOTH windows, so
   re-runs with a different predecessor never silently overwrite a different
   analysis. Manifest v3 for the cdc tree is deferred (derived artifact,
   rebuildable; the report carries sha256-free counts and file labels).
6. **Contract and budget unchanged.** No new dependency (stdlib + PyYAML,
   already in use). Exit codes 0/1/130; new `[CDC]` log tag, additive to
   ADR-0009 exactly as `[DQ]` was in ADR-025 (no §26 trigger). The ledger
   identity `inserts − deletes ≡ rows_curr − rows_prev` is asserted before
   every success — a cheap, permanent reconciliation guard on the diff
   engine itself. The DQ suite (103 tests) is untouched; CDC ships its own
   test files.

**Alternatives considered.**
- In-generator incremental emission now: INSERT/UPDATE/DELETE decided while
  rows are generated. Rejected for 6b — needs core pipeline + writer access,
  cannot be sandbox-verified under the bootstrap delivery model, and couples
  the stream to the generator's internals. Registered as the future upgrade
  path (the JSONL format is chosen so those events can slot in unchanged).
- duckdb/SQL diffing: a new dependency and a paradigm shift for a two-file
  key-diff. Rejected (same reasoning as ADR-028's duckdb alternative).
- Field-delta-only payloads (no images): smaller files, but consumers cannot
  replay state from the log alone and every schema change bites. Rejected.
- CSV change log: quoting churn and no stream semantics; JSONL is the
  NDJSON writer's existing family (ADR-0017) and streams line-by-line.
  Rejected.
- Hard-require a previous window: breaks the first run against an existing
  tree for no informational gain. Rejected (initial-load default, `error`
  available).

**Consequences.**
- The change stream is derivable at any time from any two windows of any
  tree the platform wrote — including `data/corrupted`, where drift handling
  (decision 4) applies.
- Initial loads on big entities produce one event per row (all six entities
  ≈ 23k events ≈ a few MB of JSONL); wire `cdc.entities` selectively if
  that matters, and prefer delta runs once a second window exists.
- A future DQ check can consume the cdc tree as a new surface (e.g. "every
  current row appears as an INSERT or UPDATE after-image") — registered as
  backlog synergy with the 7-track, not part of 6b.
- If the engine later grows true incremental emission, the JSONL schema
  (decision 2) is the contract both producers write; `seq` then becomes the
  merge key instead of a per-run index.

---
## ADR-030 — Milestone 6c: event stream replay + export (tracker Phase 6, event streams)

**Status:** Accepted (Milestone 6c — new `src/core/stream/` module, no existing files modified)

**Context.** After Milestone 6b (ADR-029) the platform owns three temporal
views of the same data: per-window state slices (6a windows), state DIFFS
between consecutive windows (6b CDC journals), and nothing that proves the
journals are actually REPLAYABLE or consumable as a stream. The roadmap's
Phase 6 names "event streams". The defining property of an event stream —
as opposed to a log file — is that state is a function of events:
`state == fold(events)`. The 6b journals were designed for this (full row
images, per-window `seq`, deterministic order), but nothing exercised it.
The delivery constraint is unchanged since ADR-025: streaming-side modules
integrate through documented, stable contracts (output-tree layouts +
config files) and are verified without access to the generator's internals.

**Decision.**
1. **The CDC journal chain IS the event stream; 6c makes it operational.**
   `src/core/stream/` consumes `data/cdc` (journals, read-only) and
   `data/generated` (snapshots, read-only) and produces `data/streams`
   (derived). Zero imports from generator, writer, or impurity internals.
   True in-generator event sourcing (hooks inside the pipeline, transaction
   ordering) remains the registered future upgrade path, exactly as in
   ADR-029: the JSONL contracts are chosen so a future native producer can
   slot in without changing consumers.
2. **Replay is a strict ordered fold.** Events are ordered by
   `(window, seq)` — ISO window keys make lexicographic order chronological.
   Fold semantics: INSERT sets state[key] = after (key must NOT be live —
   a live key was never deleted, so an INSERT would mean a forked chain);
   UPDATE replaces state[key] with the full after image (key must be live);
   DELETE removes the key (must be live; `after` must be null — the
   tombstone). Resurrection is legal: a DELETE frees the key, so a later
   INSERT is a fresh birth. Every contract violation fails fast naming the
   journal file and line. Column order for replayed rows comes from the
   first full row image; later images must carry the same column SET (order
   differences tolerated, set differences fail fast). Duplicate
   `(window, seq)` positions fail fast — two events cannot own one offset.
3. **Replay is VERIFIED against the snapshots.** After folding each window
   of the chain, the folded state is compared with the platform's snapshot
   for that window: key sets and per-key cell values (every cell a str, the
   ADR-028 reader-boundary). `state == fold(events)` row-for-row is the
   invariant; up to 5 sample mismatches are reported. A missing snapshot is
   recorded as `match=null` (unverified — never a silent pass); a real
   mismatch fails the run (exit 1). Byte-level CSV identity is an
   INFORMATIONAL extra (`options.byte_compare`, default off): row order and
   CSV dialect belong to the generator's writer, not to the replay
   contract.
4. **Export: one append-only, offset-addressed log per entity.** The chain's
   events are re-emitted in (window, seq) order as
   `data/streams/{entity}/part-00000.jsonl`, one envelope per line:
   `{"offset": 7, "key": "o-3", "op": "UPDATE", "entity": "order",
     "window": "2026-01-16", "seq": 3, "value": {...full after image...}}`
   with `value=null` on DELETE (tombstone convention). Offsets are 1..N
   across the WHOLE chain and are recomputed from the journal set on every
   run — deterministic, byte-identical re-runs, no state file to corrupt.
   `offsets.json` records the per-window ranges as a consumer checkpoint.
   Topic compaction (latest-wins keyed view) is deliberately deferred: the
   replayed state IS the compacted view; exporting it is a registered
   future option.
5. **Output layout, guard, report.** `data/streams/{entity}/part-00000.jsonl`
   + `data/streams/{entity}/offsets.json` + `data/streams/report.json`
   (REPORT_VERSION 1, atomic .tmp + os.replace). The runner refuses any
   `output_root` that overlaps the journal root or the snapshot root in
   either direction (contains or is contained), mirroring the 6b guard.
   Journal chains are validated before folding: exactly one initial journal
   (its absence fails with a "run cdc_main.py --window <earliest>" hint),
   deltas linked prev→window without gaps (a gap fails with the windows
   present and the filling command), partition-name/filename window
   mismatches and unclassifiable filenames fail fast. An entity with NO
   journals is skipped with a log line and report entry — a legitimate
   state (cdc_main was never run for it), not an error; a missing journal
   ROOT fails the run (the operator probably pointed at the wrong tree).
6. **Modes, contract and budget unchanged.** `--mode both` (default) =
   verify + export; `replay` = verify only, writes no stream files;
   `export` = fold + export, skips snapshot verification (the fold's strict
   contract checks still run). No new dependency (stdlib + PyYAML). Exit
   codes 0/1/130; new `[STREAM]` log tag, additive to ADR-0009 exactly as
   `[DQ]` and `[CDC]` were (no §26 trigger). The DQ suite (103 tests) and
   CDC suite (39 tests) are untouched; 6c ships its own test files.

**Alternatives considered.**
- Native in-generator event sourcing now: rejected for 6c for the ADR-029
  reasons (core pipeline access, unverifiable under the bootstrap delivery
  model). The journal-chain design means nothing consumer-side changes when
  the native producer arrives.
- Publish to a real broker (Kafka/Mosquitto): a new dependency and an
  external service; the file-based offset log models the same semantics
  (append-only, offset-addressed, tombstones) with zero ops burden. A
  future `--sink` option can bridge to a broker using the same envelopes.
- duckdb/SQL materialize-and-compare: a new dependency for a keyed-dict
  fold and a set compare. Rejected (same reasoning as ADR-028/029).
- Field-delta replay (apply only `changed` columns): the journals already
  carry full images (ADR-029 decision 2), so image-replacement is simpler
  and strictly more robust; delta-only replay would additionally require
  every producer to keep `changed` complete forever. Rejected.
- Verify only the LAST window instead of every prefix: cheaper, but prefix
  verification localizes a mismatch to the exact window where the chain and
  the snapshots diverged — the whole point of the invariant. Kept.

**Consequences.**
- `stream_main.py` after every `cdc_main.py` run is the standing end-to-end
  audit: if the journals and the snapshot tree ever disagree — writer bug,
  manual edit, partial copy — the replay names the window and the keys.
- The exported `part-00000.jsonl` gives downstream projects (Phase 7 scale
  work, ingestion drills) a realistic stream fixture without any broker:
  ordered, offset-addressed, tombstoned, deterministic.
- Large initial loads fold in memory ({pk: row} dicts; 23k rows ≈ a few MB)
  — fine at this scale; a spill-to-disk fold is a registered future need,
  not a current one.
- Synergy backlog: a DQ check over `data/streams` (e.g. offsets contiguous,
  tombstones never resurrect inside one window) can now consume a third
  derived tree the same way 7-track checks consume the others.

---
## ADR-031 — Milestone 6d: topic compaction export (tracker Phase 6, event streams)

**Status:** Accepted (Milestone 6d — adds `compact.py` + `compact_main.py` inside the
existing `src/core/stream/` package; no existing files modified)

**Context.** Milestone 6c (ADR-030) made the CDC journal chain operational as an
event stream: `data/streams/{entity}/part-00000.jsonl` is an append-only,
offset-addressed log with tombstoned DELETEs, and `state == fold(events)` is
verified against the snapshot tree. ADR-030 decision 4 deliberately deferred the
topic-compaction view: "the replayed state IS the compacted view; exporting it is
a registered future option." Downstream consumers now exist on paper (Phase 7
scale work, ingestion drills) and they want the KEYED CURRENT STATE — the
KTable analogue — without folding N events themselves. The delivery constraint
is unchanged since ADR-025: stream-side modules integrate through documented,
stable contracts (output-tree layouts + config files) and are verified without
access to the generator's internals.

**Decision.**
1. **Compaction is a log transformation, not a journal reader.** The new
   `src/core/stream/compact.py` consumes ONLY the 6c stream log
   (`data/streams`, read-only) plus the snapshot tree as its verification
   yardstick (`data/generated`, read-only), and produces
   `data/streams_compacted` (derived). It never reads `data/cdc` — the log is
   the input contract, so a future native producer (registered future in
   ADR-029/030) feeds compaction unchanged. Zero imports from generator,
   writer, or impurity internals; reuses `check_containment`,
   `snapshot_candidates`, `read_snapshot` from the 6c runner (same package,
   proven code, no duplication).
2. **Retention rule: latest-wins per key, Kafka log-compaction semantics.**
   For each key the record with the HIGHEST source offset is retained; the
   compacted log is the retained subsequence re-emitted in ascending source
   offset order, with each retained line byte-identical to its source line
   (envelope, offset and all — a compacted record is a source record, never a
   re-derivation). DELETEs retained by default: a key whose latest record is a
   tombstone stays in the log as `value=null` so consumers learn about the
   deletion. `options.drop_tombstones: true` instead drops such keys entirely,
   turning the output into a pure live-state table.
3. **Source integrity is validated before folding.** Every log line must be a
   JSON object satisfying the ADR-030 wire contract (op in {INSERT, UPDATE,
   DELETE}; key/window non-empty strings; positive integer seq; INSERT/UPDATE
   carry a full `{column: str}` image whose `primary_key` cell equals the
   envelope key; DELETE carries `value=null`; envelope entity matches the
   directory). The per-line offset must equal the line number — any gap,
   duplicate, or reorder fails fast naming the file and line. The
   `offsets.json` checkpoint left by 6c must agree (`events`, `last_offset`);
   disagreement fails with a "re-run stream_main.py" hint. An entity tree
   that is half-present (part file xor offsets.json) fails; a wholly absent
   entity directory is a legitimate skip (stream_main was never run for it).
4. **State verification: compacted(log) == state_at(log's last window).**
   The log's last window is the lexicographic max over envelope windows. The
   compacted LIVE view (non-DELETE retained records) is compared with the
   snapshot of that window — key sets and per-key cell values (`{column: str}`,
   the ADR-028 reader boundary), up to 5 sample mismatches. A missing snapshot
   is recorded as `match=null` (unverified — never a silent pass); a real
   mismatch fails the run (exit 1) and names the samples. This closes the
   loop across all three layers: journals fold to state (6c), the log
   compacts to the same state (6d), and both agree with the snapshots the
   platform wrote. `options.verify_state: false` disables the check.
5. **Output layout, guard, report.** `data/streams_compacted/{entity}/
   part-00000.jsonl` + `data/streams_compacted/{entity}/offsets.json`
   (checkpoint: records, retained, live_keys, tombstones, source_last_offset,
   last_window) + `data/streams_compacted/report.json` (REPORT_VERSION 1,
   atomic .tmp + os.replace). The containment guard refuses any
   `output_root` that overlaps the stream root or the snapshot root in either
   direction (reusing the 6c guard).
6. **Contract unchanged.** No new dependency (stdlib + PyYAML). Exit codes
   0/1/130; new `[COMPACT]` log tag, additive to ADR-0009 exactly as `[STREAM]`
   was (no §26 trigger). The DQ (103), CDC (39) and 6c stream (42) suites are
   untouched; 6d ships its own test file.

**Alternatives considered.**
- Key-sorted table output (`{key: row}` JSON): friendlier for table lookups,
  but it abandons log semantics (no offsets, no tombstones, no ordering
  guarantees) and is trivially derivable by any consumer from the compacted
  log. Rejected; a key-sorted renderer remains a registered future option.
- Compact inside 6c's export (single pass): couples two concerns and would
  not work on logs produced by any other compliant producer. The separate
  transformation keeps each stage independently verifiable and idempotent.
- Drop tombstones by default: matches "current state" intuition but silently
  hides deletions from consumers that matter (audits, CDC mirrors). Kept
  opt-in.
- Verify against the LATEST snapshot of the entity instead of the log's last
  window: false-fails in the legitimate mid-pipeline state where a new window
  was generated but cdc_main/stream_main have not caught up. The log's own
  last window is the only reference that makes compacted(log) well-defined.
- Read the journals directly and skip the log: duplicates 6c, bypasses the
  log contract, and would hide a corrupted stream log instead of exposing it.
- duckdb/pandas for the fold: a new dependency for a dict comprehension.
  Rejected (same reasoning as ADR-028/029/030).

**Consequences.**
- `data/streams_compacted/{entity}/part-00000.jsonl` is a realistic
  state-table fixture (ordered, offset-addressed, tombstone-aware) for
  Phase 7 ingestion/scale drills — no broker, no database.
- The offsets.json cross-check makes 6d a standing integrity audit of the
  6c log: a truncated, hand-edited, or partially copied stream log fails
  with file:line precision instead of propagating silently.
- Compaction holds one {key: record} map in memory (23k keys ≈ a few MB) —
  fine at this scale; spilling is a registered future need.
- Synergy backlog: DQ checks over `data/streams_compacted` (e.g. live keys
  unique, no tombstone with value) can consume the fourth derived tree the
  same way the 7-track checks consume the others.

