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
