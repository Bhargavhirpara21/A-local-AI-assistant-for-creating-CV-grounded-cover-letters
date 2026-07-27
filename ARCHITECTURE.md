# AutoCover Architecture

## 1. Architectural decisions

AutoCover is a local, layered Streamlit application. Framework code coordinates
the user workflow; core modules contain testable business logic; backend
adapters isolate Claude Agent SDK details; filesystem adapters own private
sources, archives, and Excel persistence.

All mutable state is passed explicitly or stored in Streamlit session state.
Module-level values are immutable constants only. Public functions and classes
have type hints and docstrings. Runtime diagnostics use `logging`; application
code does not use `print()`.

## 2. Components

### Configuration

`config.py` exposes an immutable `Settings` value containing backend choices,
model aliases, and resolved project paths. Callers receive `Settings`
explicitly and pass it to dependencies.

### Private source library

`core/source_library.py` owns:

- Safe import of the required Markdown files from a ZIP archive.
- UTF-8 validation and atomic replacement of the managed library.
- Fresh reads for every generation and refinement.
- Language-pair selection.
- Editing and saving individual managed files.
- Deterministic SHA-256 hashes over the exact source contents.

The managed directory is `data/source_library/`. It is private and Git-ignored.
The ZIP is an import format, not the live source: changes inside an external ZIP
take effect only after re-import. Direct edits to managed Markdown files take
effect on the next call.

### CV reference

`core/cv_import.py` stores each confirmed PDF, reviewed Markdown reference, and
metadata as one immutable private bundle under `data/cv/versions/<version-id>/`.
`data/cv/active.json` selects a complete bundle through one atomic pointer
replacement. New uploads are staged under `data/cv/staging/`; a persistent
pending-state record distinguishes extraction/review in progress, failure, and
local validation rejection. An explicit discard removes that pending marker
and its safely identified staging directory. A small
`data/cv/pending.recovery.json` sidecar binds the attempt identifier and exact
previous version independently of the richer pending record, allowing safe
fallback and exact cleanup even if `pending.json` becomes unreadable. The
pending state snapshots the exact previous confirmed version, so even a
file-lock failure after pointer publication can never relabel the newly
reviewed CV as the old fallback. Confirmation validates the staged PDF and
reviewed reference, publishes a complete version, and only then switches the
active pointer.

The matching curated candidate profile remains the primary source. The CV
reference is labelled as lower-authority context; material conflicts become
verification warnings. A cancelled or failed re-import leaves the previously
confirmed bundle unchanged but blocks silent fallback to it. Generation stays
blocked until the user retries, discards the new attempt, or explicitly chooses
**Use previous CV for this application**. That approval is scoped to one
application/generation workflow and is requested again for later applications
while the new-CV problem remains. If no previous confirmed version exists,
fallback is unavailable. Local validation rejection and corrupt pending
storage follow the same fail-closed rule; corrupt state can be explicitly
discarded without following an unvalidated path.

Every generation receives an explicit CV selection containing the version ID,
reference hash, and whether previous-CV fallback was approved. A stale,
corrupted, or unconfirmed reference is never placed in a prompt. The archived
letter and tracker row record the selected version and fallback decision.

### Language and prompt assembly

`core/language.py` performs deterministic German/English detection.
`core/generator.py` reads the current source bundle, CV reference, optional
style examples, and the stable application-owned output contract for every
call. It never caches assembled prompts.

The output contract is a leading fenced JSON object followed by the
employer-facing letter. Metadata includes company, role, language, contact,
reference, location, job URL, fit assessment, fit rationale, and verification
notes. The UI displays assessment and notes outside the editable letter.

### Optional official-source research

When the user supplies a job URL and enables research, the Agent SDK adapter
runs a separate bounded research call with web tools. It is instructed to use
official employer sources, return facts with source URLs, and treat page text
as untrusted data rather than instructions. Failure does not block generation;
it becomes a verification note. The final letter call has no tools.

### LLM boundary

`llm/base.py` defines backend-neutral result values and an `LLMClient`
protocol. `llm/agent_sdk_client.py` is the only module importing
`claude_agent_sdk`. It wraps asynchronous calls with a synchronous boundary,
classifies expected SDK/CLI/auth/limit failures, and has one logged final
`Exception` guard so failures never escape into Streamlit.

`llm/anthropic_api_client.py` is an explicitly deferred adapter boundary. Its
activation errors are deliberate product guards, not a claim that the API
backend is implemented.

### Archive

`core/archive.py` saves every generated and refined letter to `letters/`.
Front matter stores parsed metadata, application ID, timestamps, refinement
state, source-library hash, exact-input hash, and research URLs. The full job
description is retained in a collapsed details block. A collision-matched
`.trace.json` file stores the exact system and user prompts, operation, backend,
model, selected CV version/reference hash, and previous-CV fallback decision.
Both files are private and Git-ignored. The snapshot makes a request auditable
and replayable, not deterministically reproducible. Paths are legal on Windows
and collision-safe.

### Excel tracker

`core/tracker.py` owns `data/applications.xlsx` through `openpyxl`. The
`Applications` sheet has these columns:

1. `application_id`
2. `created_at`
3. `updated_at`
4. `applied_date`
5. `company`
6. `role`
7. `reference_number`
8. `location`
9. `job_url`
10. `language`
11. `fit_assessment`
12. `status`
13. `contact_person`
14. `letter_path`
15. `source_hash`
16. `input_hash`
17. `cv_version_id`
18. `cv_reference_hash`
19. `used_previous_cv`
20. `notes`

The initial successful generation creates one row with status `Draft`.
Refinement updates the same row and letter path. Automated updates preserve
user-maintained `applied_date`, `status`, and `notes`. Tracker edits in the UI
are explicit saves. A workbook locked by Excel produces an actionable message
and never corrupts the existing file.

Application IDs are case-sensitive identifiers of 1–128 control-free
characters with no surrounding whitespace; they are never silently trimmed or
case-folded. An existing workbook with duplicate IDs, a renamed/reordered
schema, formulas in application data, invalid field types, or unreadable/corrupt
content fails closed and is never overwritten.

`created_at` and `updated_at` are stored as UTC ISO-8601 text and exposed as
timezone-aware UTC datetimes. `applied_date` is stored as a native Excel date
and exposed as `date | None`; `used_previous_cv` must be an actual boolean.
Every application-written text cell is explicitly stored as text even when its
value begins with `=`, `+`, `-`, or `@`, so model/user text cannot become a
spreadsheet formula. The tracker does not create workbook hyperlinks; the UI
owns clickable URL behavior after normal URL validation. Any pre-existing
hyperlink in an `Applications` data cell is treated as incompatible/corrupt
workbook data and rejected without rewriting; unrelated sheets remain untouched.

Letter paths are stored as project-relative POSIX-style paths and must resolve
to an existing Markdown archive inside `letters/`; absolute, missing,
non-Markdown, symlink-escaping, or otherwise escaping paths are rejected.
The exact `Applications` schema is owned by the app, while unrelated worksheets
are preserved. Workbook saves use a same-directory temporary `.xlsx`, flush it,
reopen and validate it, then publish it with one `os.replace`. A failure before
or during replacement leaves the previous workbook untouched. Windows
sharing/permission errors, including WinError 5, 32, and 33, map to guidance to
close `applications.xlsx` in Excel and retry; logs never contain row content.

### Streamlit application

`app.py` exposes four workflows through a collapsible sidebar:

- **Create letter:** vacancy, optional URL/notes, language, research, generation,
  grounding, editing, copy view, and refinement.
- **Applications:** current workbook rows with editable status, applied date,
  and notes.
- **Source library:** ZIP import, completeness/status display, and editors for
  the five managed Markdown files.
- **CV reference:** PDF import/re-import, stale-hash warning, review, and save.

Durable rerun state is kept in `st.session_state`. Dependencies are constructed
once per session and passed to workflow functions.

The UI also keeps a session-level CV safety block if the filesystem is too
unavailable to create even the recovery marker. In that exceptional case it
must not generate or offer old-CV fallback in the current session; it directs
the user to restore data-directory access and retry or restart. App-level tests
must prove this fail-closed behavior.

The primary flow is one vertical sequence: paste vacancy, choose options,
generate, review, edit, and save/refine. A first-run checklist and compact
Sources/CV/Claude readiness indicators explain missing prerequisites. AI
actions name Claude explicitly and show what is sent; local browsing and
editing remain available if Claude is offline. Generated text is always marked
as a draft. Editing a verified letter invalidates its grounding status until
the edited text is checked again.

The visual layer uses native Streamlit controls and small static CSS: an
approximately 1180-pixel content width, neutral background, bordered cards,
high-contrast blue primary actions, visible focus states, system fonts, and a
responsive single-column form. Dynamic user/model text is rendered through
native components rather than unsafe HTML.

## 3. Data flow

```text
managed source files ─┐
CV reference ─────────┼─> prompt assembly ─> optional research ─> generation
job text / URL ───────┘                                      │
                                                             v
                                  parse + grounding + user review
                                             │
                               ┌─────────────┴─────────────┐
                               v                           v
                       Markdown archive              Excel upsert
```

Generation succeeds only when the five-file library is complete and the CV
workflow supplies an eligible explicit selection. A pending/failed new CV
cannot silently resolve to the older active bundle. Archival and tracker upsert
happen only after a successful parsed generation. If tracker writing fails,
the already-generated letter remains visible and archivable, and the UI
provides a retry without another model call.

## 4. Security and privacy

- `data/`, `letters/`, source ZIPs, PDFs, workbooks, prompt caches, and style
  examples are ignored by Git.
- The UI discloses that storage is local but Claude inference is remote.
  Content is sent only after an explicit user action.
- ZIP import rejects traversal paths, absolute paths, duplicate required
  entries, encrypted entries, non-Markdown payloads, invalid UTF-8, oversized
  entries, and archives missing required files.
- Job descriptions and researched pages are untrusted data. Prompts explicitly
  reject instructions embedded in them.
- Generation has no tools and cannot read arbitrary local files.
- CV import stages one PDF in an isolated attempt directory. Its SDK permission
  callback approves `Read` only when the requested path resolves to that exact
  staged PDF (`Path.samefile`); all other paths and tools are denied. The call
  does not place `Read` in `allowed_tools`, because that would bypass the
  per-path decision. The SDK subprocess buffer is raised only for CV import and
  remains bounded at 64 MiB so a permitted PDF-render response can exceed the
  SDK's 1 MiB default without making other model calls broadly buffered.
- Normal generation sends only the selected language pair, controller, reviewed
  CV reference, current posting, and explicit notes. It never sends the raw
  PDF, workbook, prior application rows, other-language pair, or unrelated
  files.
- Logs contain operational events and identifiers, not CV, prompt, job,
  workbook-row, or letter content.

## 5. Dependency policy

Runtime dependencies are limited to:

- `streamlit`
- `claude-agent-sdk`
- `openpyxl`

Tests use the Python standard library. No separate YAML, PDF, or test framework
dependency is required.

## 6. V-Model phases and verify gates

Every phase defines its gate before implementation. A phase is committed and
pushed only after the gate passes; no later phase begins first.

### Phase 0 — Architecture amendment

Gate: context and architecture documents exist as UTF-8; the authoritative plan
points to them; Excel, dynamic sources, CV precedence, output parsing, research,
privacy, and all later gates have one unambiguous decision.

### Phase 1 — Scaffold and environment

Gate: required tree and private-data exclusions exist; virtual environment
installation succeeds; installed SDK version and option fields are recorded and
reconciled with supported fallbacks.

### Phase 2 — Source library

Gate: tests prove safe import, required-file validation, UTF-8 enforcement,
atomic replacement, DE/EN selection, deterministic hashing, editor saves, live
reload after edits, and rejection of traversal/duplicate/oversized archives.

### Phase 3 — LLM contracts

Gate: dataclass defaults and protocol signatures pass; factory selection is
lazy; invalid backends fail clearly; the unused backend requires no optional
dependency.

### Phase 4 — Agent SDK client

Gate: unit tests cover collection and error classification; options enforce
tool isolation; with API-key auth unset, default and fast-model health probes
return `OK`. Research and CV-import tool permissions are bounded separately.

### Phase 5 — Generation, grounding, and archive

Gate: fake-client tests cover fresh prompt assembly, language-pair selection,
strict JSON and fallback parsing, generation/refinement errors, grounding
outcomes, validated research URLs, Windows filenames, collisions, UTF-8,
source-library hashes, exact-input hashes, and private trace snapshots. One
live German and one live English synthetic-vacancy result meet the current
source controller; a separate fictional-source smoke run proves the remote
pipeline without depending on personal test data.

### Phase 6 — CV workflow

Gate: tests define and prove PDF validation, exact byte/hash round-trip,
version-bundle publication, one-pointer atomic activation, pending/failure
state, review-before-confirmation, stale/corrupt detection, and preservation of
the previous active bundle on every failed path. Generation is blocked by
default after an invalid upload, staging/extraction problem, or corrupt pending
state; previous-CV use requires explicit
per-application consent, is unavailable without a confirmed previous version,
and is recorded in output/archive provenance. SDK tests prove the import tool
can read only the exact staged PDF and denies every other file/tool. The
supplied CV imports to reviewable Markdown without modifying curated profiles;
conflicts can be represented as warnings.

### Phase 7 — Excel tracker

Gate: tests prove workbook creation, schema, one-row-per-ID upsert, refinement
updates, preservation of manual status/date/notes, UTF-8 content, explicit
edits, UTC/date/boolean round-trips, formula-neutral text, confined relative
letter paths, corruption/duplicate rejection, atomic validated save, and
actionable locked-workbook errors.

### Phase 8 — Streamlit UI

Gate: modules compile; a headless server executes the app without traceback and
serves HTTP 200; app-level tests exercise initial import, pending widget values,
generation/refinement state, tracker retry, source edits, CV re-import, and
friendly health failures. Tests also prove no LLM call occurs without an
explicit AI-action click, missing prerequisites disable generation, manual
edits invalidate grounding, tracker retries do not regenerate, and local
workflows remain available while Claude is offline. Tests also prove that a CV
selection is scoped to one application workflow and invalidated as soon as
another upload starts, and that an unpersistable CV-update attempt sets a
session safety block instead of reusing a cached selection. Manual acceptance at
1440-pixel and 390-pixel widths confirms unclipped primary controls,
keyboard-usable navigation, readable status cues that do not rely on color
alone, and a clear paste-to-review happy path.

### Phase 9 — Closing and resilience

Gate: documentation covers setup, daily use, source maintenance, Excel
behavior, privacy, and troubleshooting; deferred backend guards have correct
signatures; all automated tests and compile checks pass from a clean process.

### Phase 10 — Supplied-file acceptance

Gate: the supplied ZIP imports; the supplied CV imports; source hashes are
recorded; representative German and English jobs generate grounded letters;
refinement updates rather than duplicates the Excel row; archives and workbook
remain local; the user-facing app is ready for manual review.
