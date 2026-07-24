# AutoCover v1 — Implementation Guide (authoritative)

**Read this entire file before writing any code, then execute Part C in order.**

## Approved scope amendment — 24 July 2026

The user's requirements supplied on 24 July 2026 supersede conflicting v1
decisions below. The detailed component design and revised V-Model gates are
defined in `ARCHITECTURE.md`; product rationale is in `THESIS_CONTEXT.md`.

The approved changes are:

1. The five Markdown files in the supplied Bhargav Cover Letter System are an
   editable private source library. The application imports them into
   `data/source_library/`, reads them fresh for every generation or refinement,
   and selects the matching German or English profile and master library.
2. The supplied drafting controller and matching-language files take
   precedence over conflicting wording, length, structure, and source-authority
   rules in Appendix 1. A small application-owned JSON envelope is added only
   so metadata, fit assessment, verification notes, and the letter can be
   parsed reliably.
3. The supplied CV is imported as a secondary reference. The curated
   language-specific candidate profile remains the primary candidate-fact
   source; conflicts are surfaced rather than resolved silently.
4. Excel application tracking is now in v1. The application owns
   `data/applications.xlsx`, creates one row per generated application, updates
   that same row on refinement, and preserves user-maintained status, applied
   date, and notes.
5. `openpyxl` is an approved dependency for the local `.xlsx` workbook.
6. Official-source vacancy/company research may run as a separate,
   user-controlled step when a job URL is supplied. Letter generation itself
   remains tool-free and receives only the resulting bounded research summary.
7. Personal source files, imported CV data, generated letters, cached prompts,
   and the Excel workbook remain local and Git-ignored.
8. The revised implementation order is the phase sequence in
   `ARCHITECTURE.md`. Each phase has an explicit verify gate and is committed
   and pushed before the next phase starts.
9. Installed Agent SDK 0.2.126 bundles a native Windows `claude.exe` and
   rejects `.cmd`/`.bat` values for `cli_path`. Keep `cli_path=None` to use the
   bundled executable, or configure a native `.exe`; the older `.cmd` fallback
   guidance later in this original guide is superseded.

Where this amendment or `ARCHITECTURE.md` conflicts with a later section of
this original guide, the amendment and architecture take precedence. Sections
that do not conflict remain authoritative.

This guide was produced in a dedicated planning session on 2026-07-22, with the
Claude Agent SDK surface verified against its live docs (v0.2.125) and this
machine's environment verified directly. It is written to be executed by a
fresh Claude Code session with no other context. It is the single source of
truth for v1 and supersedes any shorter plan summaries.

## Rules for the implementing session

1. The product decisions in Part A are **locked with the user** — do not
   re-litigate, trim, or expand them.
2. Follow the build steps in Part C **in order**. Each step ends with a verify
   gate — do not continue past a failing gate; fix it first.
3. The appendices contain **verbatim contents** for all non-code files
   (prompts, config, requirements). Copy them exactly — their wording is part
   of the design, especially the prompt files.
4. If the installed `claude-agent-sdk` differs from the verified surface in
   §B3, apply the fallbacks in §B4. Do not redesign.
5. Every file read/write must pass `encoding="utf-8"` explicitly (German
   umlauts appear in letters, filenames, and the CV profile).
6. Add **no** dependencies beyond `requirements.txt` and **no** features beyond
   this document (the deferred list in §A4 is deferred on purpose).
7. The only things to request from the user are listed in §C0.
8. Report each verify-gate result to the user as you go; if a gate needs their
   input (e.g. their CV PDF), pause and ask.

---

# Part A — Product context and locked decisions

## A1. Why this exists

The user is finishing a master's thesis and actively applying for full-time
jobs in Germany. Today every application means manually feeding CV + job
description to an AI for a cover letter, then manually logging the application
in Excel, then manually watching email for status updates. v1 automates only
the first and most time-consuming step:

> **Paste a job description → get a customized, fact-grounded cover letter.**

Tracking and email features come later; the architecture must not block them.

## A2. Locked decisions (agreed with the user — do not revisit)

| Decision | Value |
|---|---|
| Interface | Local **Streamlit** web app, runs on the user's laptop only (`streamlit run app.py`, browser at localhost). €0 hosting. |
| AI backend | The user's **Claude subscription** via the **Claude Agent SDK for Python** (`claude-agent-sdk`), which drives the installed, logged-in Claude Code CLI. **No API key in v1, no separate bill.** A swappable client interface prepares a later Anthropic-API backend (`claude-opus-4-8`) enabled by one config change. |
| Languages | **German and English** letters. Auto-detect the target language from the job description, manual override in the UI. German letters follow Anschreiben conventions. |
| v1 output | Letter shown on screen as editable text with a copy affordance. Every letter silently archived as markdown under `letters\`. |

## A3. Core quality mechanisms (the product's soul)

- `prompts/instructions.md` — user-editable master instructions: structure,
  length, DE/EN conventions, banned AI-cliché phrases, strict grounding rules.
  Full text in Appendix 1.
- **Grounding**: letters may only claim facts present in `data/cv_profile.md`
  (created once from the user's CV PDF, reviewed by the user). The job ad is
  never a source of facts about the candidate.
- **Grounding check**: optional second AI pass that lists any claim in the
  letter not backed by the CV profile; shown as warnings in the UI.
- **Style examples**: the user can drop 1–2 past letters into
  `prompts/style_examples/` as voice references.
- **Refine loop**: free-text feedback ("shorter, emphasize thesis")
  regenerates the letter while keeping the user's manual edits.

## A4. Explicitly out of scope for v1 (deferred, do not build)

DOCX/PDF export · Excel/CSV application log · email status checking ·
hosting/multi-user · token streaming into the UI (v1.1 candidate).

---

# Part B — Technical foundation (verified 2026-07-22)

## B1. Environment (verified on this machine)

- Windows 11, PowerShell. Project dir: `C:\Users\fk6147\AutoCover`.
- Python **3.12.10** (`python` on PATH), pip 25.0.1. SDK requires ≥3.10 — OK.
- Claude Code CLI **2.1.185** installed at
  `C:\Users\fk6147\AppData\Roaming\npm\claude.cmd`, logged in with the user's
  subscription.

## B2. Verified Claude Agent SDK facts (v0.2.125)

| Item | Verified answer |
|---|---|
| Package | `claude-agent-sdk` on PyPI; `pip install claude-agent-sdk` |
| Core API | `query(*, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]` — **async only**; each call spawns a fresh CLI subprocess and session |
| Pure text call | `ClaudeAgentOptions(tools=[])` → model has no tools at all (letter generation) |
| PDF reading | `ClaudeAgentOptions(tools=["Read"], allowed_tools=["Read"])` — Claude Code's `Read` tool renders PDFs natively. **No pypdf/PDF library needed anywhere.** `allowed_tools` pre-approves so nothing blocks on permissions |
| System prompt | `system_prompt` accepts a plain string **or** `{"type": "file", "path": "<abs path>"}`. Use the file form for large prompts — Windows has a ~32 KB command-line limit |
| Isolation | `setting_sources=[]` prevents the user's personal `~/.claude/CLAUDE.md`, project settings, and skills from leaking into letter generation. Always pass it |
| Auth | With `ANTHROPIC_API_KEY` **unset**, the spawned CLI uses the existing Claude Code subscription login. This is the whole point of the backend choice |
| Model | `model=None` inherits the CLI's configured default. Aliases `"sonnet"`, `"opus"`, `"haiku"` accepted |
| Result stream | Messages include `AssistantMessage` (with `content: list` containing `TextBlock(text=...)`) and a terminal `ResultMessage` with `result: str | None`, `is_error: bool`, `subtype`, `total_cost_usd`, `session_id`, and possibly `errors` / `api_error_status` (access defensively via `getattr`) |
| Errors | `CLINotFoundError` (CLI missing), `CLIConnectionError`, `ProcessError` (has `exit_code`), `CLIJSONDecodeError`, base `ClaudeSDKError` |
| CLI path | `cli_path=` option can point at the system CLI if auto-detection fails |

## B3. The exact SDK call shapes to implement

Letter generation (and grounding check — same shape, different system/model):

```python
_SYSTEM_PROMPT_CACHE = config.CACHE_DIR / "last_system_prompt.md"
_SYSTEM_PROMPT_CACHE.write_text(system, encoding="utf-8")
options = ClaudeAgentOptions(
    system_prompt={"type": "file", "path": str(_SYSTEM_PROMPT_CACHE)},
    tools=[],
    setting_sources=[],
    model=model or config.SDK_MODEL,          # None = subscription default
    max_turns=config.GENERATION_MAX_TURNS,
    cwd=str(config.PROJECT_ROOT),
    cli_path=config.CLI_PATH,                 # None = auto-detect
)
```

CV import (the only call that gets a tool):

```python
options = ClaudeAgentOptions(
    system_prompt=cv_import_prompt_text,      # short — plain string is fine
    tools=["Read"],
    allowed_tools=["Read"],
    setting_sources=[],
    model=config.SDK_MODEL,
    max_turns=config.IMPORT_MAX_TURNS,
    cwd=str(config.DATA_DIR),
    cli_path=config.CLI_PATH,
)
prompt = f"Read the CV PDF at {pdf_path.resolve()} and convert it following your instructions. Output only the markdown profile."
```

Collector (async; the sync facade wraps it in `asyncio.run(...)` per call —
fresh event loop every time, never cached, no `nest_asyncio`):

```python
async def _collect(prompt: str, options: ClaudeAgentOptions) -> LLMResult:
    texts: list[str] = []
    final = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    texts.append(block.text)
        elif isinstance(message, ResultMessage):
            final = message
    # Prefer final.result if it is a non-empty string; else join texts.
    # final.is_error=True -> LLMResult(is_error=True, error_message=...)
    #   (join getattr(final, "errors", None) or fall back to result/subtype;
    #    if the message mentions limit/rate, append a friendly hint that the
    #    subscription usage window may be exhausted).
    # Empty text on success -> is_error=True, "The model returned no text."
```

## B4. Fallbacks if the installed SDK differs

Before writing `llm/agent_sdk_client.py`, print the actually-available option
fields once:

```powershell
.venv\Scripts\python.exe -c "import claude_agent_sdk as s; print(sorted(s.ClaudeAgentOptions.__dataclass_fields__.keys()))"
```

| If missing | Do instead |
|---|---|
| `tools` field | Omit it; rely on `allowed_tools=[]` (nothing pre-approved → tool calls are denied in SDK mode) plus the system prompt's "output only the letter" contract |
| `system_prompt` file form rejected | Pass the assembled string directly; if a `ProcessError` about command length appears, trim style examples and note the SDK should be upgraded |
| `cli_path` | Omit it (SDK auto-detects; a bundled CLI also exists) |
| `setting_sources` | Omit it (older SDK-mode default did not load filesystem settings) |
| Any import name (`CLIJSONDecodeError` etc.) | Import the minimal set (`query`, `ClaudeAgentOptions`, `AssistantMessage`, `ResultMessage`, `TextBlock`) and map everything else via a broad `except Exception` to `LLMResult(is_error=True, ...)` |
| `"haiku"` model alias rejected at runtime | Set `GROUNDING_MODEL = None` in `config.py` (grounding then uses the default model) |

## B5. Known pitfalls and their required mitigations

- **P1 Streamlit sync vs SDK async** — public client methods are synchronous
  and internally call `asyncio.run(...)`. Windows' default Proactor event loop
  (needed for subprocess spawning) works under Python 3.12 even on Streamlit's
  worker thread. Never install a Selector policy, never reuse loops.
- **P2 Streamlit reruns wipe locals** — all durable state lives in
  `st.session_state`, initialized by one `init_state()` at the top of
  `app.py`. Widget values persist via keys.
- **P3 Latency** — a letter takes ~30–120 s on the subscription path. Wrap
  calls in `st.status(...)` with phase messages and a "typically 1–2 minutes"
  caption. Never auto-retry failed calls (it burns subscription quota).
- **P4 CLI missing / not logged in** — one `health_check()` per app session
  (cached in session state): `CLINotFoundError` → show install hint + tell the
  user to set `CLI_PATH` in `config.py` to
  `C:\Users\fk6147\AppData\Roaming\npm\claude.cmd`; auth-flavored errors
  (login/credential/401 in the message) → "open a terminal, run `claude`, type
  `/login`, then restart this app"; other errors → show the message with a
  Retry button.
- **P5 Subscription limits** — surface as `ResultMessage.is_error` or
  `ProcessError`; map to a friendly warning ("Claude subscription limit
  reached — try again after the usage window resets").
- **P6 Windows ~32 KB command-line limit** — always pass the generation system
  prompt via the `{"type": "file"}` form (also gives the user an inspectable
  record of exactly what was sent: `data\cache\last_system_prompt.md`).
- **P7 Prompt leakage** — `setting_sources=[]` on every call; the system
  prompt is always fully explicit.
- **P8 Upload handling** — `st.file_uploader` yields bytes; write them to
  `data\uploads\cv.pdf` first, then pass that absolute path to `import_cv`.
- **P9 Windows filenames & encoding** — UTF-8 everywhere; archive filename
  sanitizer strips `<>:"/\|?*` and control chars, collapses whitespace to
  `-`, keeps umlauts, truncates to 60 chars, never ends in a dot/space.
- **P10 Widget-state mutation** — Streamlit forbids setting a widget's
  session-state key after the widget rendered in the same run. Use the
  "pending value" pattern (§C8) when programmatically updating the letter
  text area, and `st.rerun()` after storing results.

---

# Part C — Build steps (execute in order)

## C0. Inputs to request from the user (only these)

1. Their **CV as a PDF** — needed at step C10 (they can also do the import
   themselves in the UI on first launch).
2. Optionally **1–2 past cover letters** they were happy with → save as
   `.md`/`.txt` into `prompts/style_examples/`.

## C1. Scaffold and environment

Create this tree (empty `__init__.py` files where shown):

```
AutoCover\
├─ app.py                      # C8
├─ config.py                   # Appendix 4 — verbatim
├─ requirements.txt            # Appendix 5 — verbatim
├─ README.md                   # C9
├─ .gitignore                  # Appendix 5 — verbatim
├─ PLAN.md                     # (this file, already present)
├─ llm\
│  ├─ __init__.py              # C3 (factory)
│  ├─ base.py                  # C3
│  ├─ agent_sdk_client.py      # C4
│  └─ anthropic_api_client.py  # C9
├─ core\
│  ├─ __init__.py              # empty
│  ├─ generator.py             # C6
│  ├─ language.py              # C5
│  ├─ archive.py               # C5
│  └─ cv_import.py             # C7
├─ prompts\
│  ├─ instructions.md          # Appendix 1 — verbatim
│  ├─ grounding_check.md       # Appendix 3 — verbatim
│  ├─ cv_import.md             # Appendix 2 — verbatim
│  └─ style_examples\
│     └─ README.md             # Appendix 5 — verbatim
├─ data\                       # created at runtime by config.ensure_dirs()
└─ letters\                    # created at runtime
```

Then:

```powershell
cd C:\Users\fk6147\AutoCover
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip show claude-agent-sdk
```

**Verify gate:** install succeeds; note the installed `claude-agent-sdk`
version; run the `__dataclass_fields__` probe from §B4 and reconcile any
missing fields against the fallback table before writing C4.

## C2. Prompt files

Write Appendices 1–3 and the style-examples README verbatim.

**Verify gate:** files exist, UTF-8, contents match the appendices.

## C3. `llm/base.py` and `llm/__init__.py`

`base.py` — backend-agnostic contract:

```python
@dataclass
class LLMResult:
    text: str
    is_error: bool = False
    error_message: str | None = None
    cost_usd: float | None = None
    session_id: str | None = None

@dataclass
class HealthStatus:
    ok: bool
    detail: str   # actionable text shown to the user on failure

class LLMClient(Protocol):
    def generate(self, system: str, prompt: str, *, model: str | None = None) -> LLMResult: ...
    def import_cv(self, pdf_path: Path) -> LLMResult: ...   # markdown profile in .text
    def health_check(self) -> HealthStatus: ...
```

`__init__.py` — `get_client() -> LLMClient` switching on `config.BACKEND`
(`"agent_sdk"` → `AgentSDKClient`, `"anthropic_api"` → `AnthropicAPIClient`,
else raise `ValueError`). Import the concrete class lazily inside each branch
so the unused backend's dependencies are never imported. **This factory is the
one-config-change swap point.**

## C4. `llm/agent_sdk_client.py` — then smoke test BEFORE any UI

The only file that imports `claude_agent_sdk`. Implement per §B3:

- `generate(system, prompt, *, model=None)` — write system prompt to cache
  file, build options (file-form system prompt, `tools=[]`), run collector.
- `import_cv(pdf_path)` — system prompt from `prompts/cv_import.md` (string
  form is fine — it is short), `tools=["Read"], allowed_tools=["Read"]`,
  `cwd=config.DATA_DIR`.
- `health_check()` — `generate("You reply with exactly: OK", "ping",
  model=config.GROUNDING_MODEL or config.SDK_MODEL)`; classify failures per
  P4 into actionable `HealthStatus.detail` strings.
- `_run(prompt, options)` — `asyncio.run(_collect(...))` wrapped in
  `try/except` for `CLINotFoundError` / `ProcessError` / `CLIJSONDecodeError`
  / `ClaudeSDKError` / broad `Exception`, each mapped to
  `LLMResult(is_error=True, error_message=<actionable text>)`. Never let an
  exception escape to the UI.

**Verify gate (critical — proves subscription auth end to end):**

```powershell
Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
.venv\Scripts\python.exe -c "from llm import get_client; r = get_client().generate('You reply with exactly: OK', 'ping'); print(('ERROR: ' + str(r.error_message)) if r.is_error else r.text)"
```

Expected output: `OK` (allow ~15–60 s). Then repeat once with
`model='haiku'` passed explicitly — if that errors, set
`GROUNDING_MODEL = None` in `config.py` (fallback table §B4).

Troubleshooting: `CLINotFoundError` → set `CLI_PATH` in config to
`C:\Users\fk6147\AppData\Roaming\npm\claude.cmd`. Auth error → run `claude`,
`/login`. `TypeError` on options → §B4 fallbacks.

## C5. `core/language.py` and `core/archive.py` (pure functions)

`language.py` — `detect_language(text: str) -> Literal["de", "en"]`:
lowercase, tokenize with `[a-zäöüß]+`, count hits against two stopword sets
(DE: der, die, das, und, für, mit, wir, sie, ist, im, den, bei, eine, als,
auf, ihre, sind, werden, aus, dem, nicht, oder, wird, über, zur, zum;
EN: the, and, with, for, you, are, our, we, will, of, to, in, is, on, as, be,
that, have, your, from, at, or, an, by). Ties or no hits → `"de"` (target
market is Germany). No LLM call, no dependency.

`archive.py` — `save_letter(out: LetterOutput, job_text: str, refined: bool = False) -> Path`:

- Filename: `{YYYY-MM-DD}_{sanitize(company)}_{sanitize(role)}` +
  (`_refined` if refined) + `_{HHMM}.md`; if the path exists, append `_2`,
  `_3`, …
- `sanitize()` per P9.
- Content: yaml front-matter (`company`, `role`, `language`,
  `contact_person` (or `null`), `generated_at` ISO timestamp, `refined`),
  then the letter, then a `<details><summary>Job description used</summary>`
  block containing the job text (so every archived letter carries its ad).
- UTF-8, `config.ensure_dirs()` first.

**Verify gate:** quick inline tests —
`detect_language` on a German and an English sample ad;
`sanitize("IT/OT GmbH & Co. KG")` yields a legal Windows filename; a fake
`LetterOutput` round-trips to a file that starts with `---` front-matter.

## C6. `core/generator.py`

Dataclasses:

```python
@dataclass
class LetterOutput:
    letter: str
    company: str = "Unknown"
    role: str = "Unknown"
    language: str = "de"
    contact_person: str | None = None
    raw: str = ""

@dataclass
class GroundingResult:
    ran: bool                 # False if the check itself failed to execute
    ok: bool
    warnings: list[str]
    raw: str = ""

class GenerationError(RuntimeError): ...
```

Functions:

- `build_system_prompt() -> str` — concatenate: `prompts/instructions.md`
  + `\n\n# CV PROFILE (sole source of truth about the candidate)\n\n`
  + `data/cv_profile.md` (raise `GenerationError("No CV profile yet — import your CV first.")`
  if missing) + optional `\n\n# STYLE EXAMPLES (match the voice, never the facts)\n\n`
  built from `prompts/style_examples/*.md|*.txt` (exclude `README.md`), each
  prefixed `## Example: <filename>`.
- `generate_letter(client, job_text: str, language: str, notes: str = "") -> LetterOutput`
  — user prompt:

  ```
  TARGET_LANGUAGE: {language}

  # JOB DESCRIPTION
  {job_text}

  # ADDITIONAL NOTES FROM THE CANDIDATE   (only if notes non-empty)
  {notes}
  ```

  Call `client.generate(system, prompt)`; on `is_error` raise
  `GenerationError(error_message)`; else `parse_letter_output(text, language)`.
- `refine_letter(client, job_text, previous_letter: str, feedback: str, language) -> LetterOutput`
  — same system prompt; user prompt adds `REFINEMENT MODE` line plus
  `# PREVIOUS LETTER` and `# FEEDBACK TO APPLY` sections. **Stateless** — no
  session resume; this keeps behavior identical on the future API backend.
- `parse_letter_output(raw: str, fallback_language: str) -> LetterOutput` —
  lenient: regex a leading fenced block ` ```yaml ... ``` ` (fence label
  optional); parse simple `key: value` lines (strip quotes; `null`/`none`/
  empty → `None` for contact_person); everything after the closing fence is
  the letter. No yaml library. If no fenced block, the whole text is the
  letter and defaults apply.
- `check_grounding(client, letter: str) -> GroundingResult` — system =
  `prompts/grounding_check.md` + `\n\n# CV PROFILE (ground truth)\n\n` +
  profile; prompt = `# COVER LETTER TO CHECK\n\n` + letter; call with
  `model=config.GROUNDING_MODEL`. Parse: strip; exactly `OK` (case-insensitive,
  optional trailing period) → `ok=True`. Else collect lines starting with
  `- `/`* `/`• ` as warnings; if none, one warning = the whole text. If the
  call errors → `GroundingResult(ran=False, ok=False, warnings=[error_message])`
  (a failed check must never be conflated with a passed one).

**Verify gate:** create a temporary throwaway `data/cv_profile.md` (clearly
fake student profile, 10–15 lines), run `generate_letter` from the terminal
with a real pasted German job ad → parses into company/role, letter is German
with correct greeting/closing; run `check_grounding` on it. **Delete the fake
profile afterwards** so the real first-run import still triggers.

## C7. `core/cv_import.py`

- `save_uploaded_pdf(file_bytes: bytes, original_name: str = "cv.pdf") -> Path`
  — write to `config.UPLOADS_DIR / "cv.pdf"`.
- `load_profile() -> str | None` — `None` if missing or blank.
- `save_profile(text: str) -> None` — UTF-8 to `config.CV_PROFILE_PATH`.

**Verify gate:** with any real PDF available (else defer to C10):
`get_client().import_cv(path)` returns markdown starting with `# Profile`.

## C8. `app.py` — the Streamlit UI

Single page. Structure and behavior:

1. `st.set_page_config(page_title="AutoCover", page_icon="✉️", layout="wide")`,
   `config.ensure_dirs()`, `@st.cache_resource` factory for `get_client()`.
2. `init_state()` — `st.session_state.setdefault` for: `health`, `reimport`
   (bool), `profile_draft`, `letter_obj`, `grounding`, `archived_path`,
   `job_text_used`, `pending_letter_text`, `error`. Then the **pending value
   pattern** (P10): if `pending_letter_text` is not None, copy it into
   `st.session_state.letter_text` and clear it — this runs before any widget
   renders, so programmatic updates of the letter area are legal.
3. **Health gate** — if `health` unset, run `client.health_check()` in a
   spinner and cache it. On failure: `st.error(detail)` + Retry button
   (clears `health`, `st.rerun()`) + `st.stop()`.
4. **First-run / re-import view** (shown when `load_profile()` is None or
   `reimport`): explainer text → `st.file_uploader` (PDF) → "Import with
   Claude" button → save bytes, `client.import_cv`, on success store text in
   `profile_draft` → render `st.text_area` (value=draft, height≈520, no key —
   auto-key keeps edits) with caption "check every employer, date and skill —
   letters can only claim what is in here" → "Save profile and continue" →
   `save_profile`, clear draft + `reimport`, `st.rerun()`. If a profile
   already exists, offer "Cancel re-import".
5. **Main view** — `st.tabs(["New letter", "CV profile"])`.

   **New letter tab:**
   - `st.text_area("Job description", height=280, key="job_text")`
   - `st.radio("Letter language", ["Auto-detect", "Deutsch", "English"], horizontal=True)`
   - `st.text_input("Optional notes for this application (referral, start date, ...)")`
   - `st.toggle("Grounding check — verify every claim against your CV", value=config.GROUNDING_ENABLED_DEFAULT)`
   - Primary button **Generate letter** (disabled while job text is blank):
     resolve language (`detect_language` when Auto), then `run_generation(...)`.
   - `run_generation(job_text, lang, notes, grounding_on, refine_feedback=None, previous_letter=None)`:
     inside `st.status` ("Calling Claude — typically 1–2 minutes…"), call
     `generate_letter` or `refine_letter`; optionally `check_grounding`
     (second phase message); `archive.save_letter`. On `GenerationError`:
     store message in `error` and return. On success: store `letter_obj`,
     `grounding`, `archived_path`, `job_text_used`,
     `pending_letter_text = out.letter`, then `st.rerun()`.
   - Below: show `error` if set. If `letter_obj` exists:
     subheader `{role} — {company}`, caption with language / contact /
     archived path; grounding banner (`ran=False` → `st.info` "check could
     not run"; `ok` → `st.success`; else an auto-expanded `st.expander`
     titled "⚠️ N claim(s) not backed by your CV — review before sending"
     with one `st.warning` per item);
     `st.text_area("Letter (editable)", height=450, key="letter_text")`;
     an expander "Copy view" containing `st.code(<current letter text>, language=None)`
     — **`st.code`'s built-in copy icon is the copy-to-clipboard affordance**
     (no pyperclip).
   - Refine row: `st.text_input` (placeholder "e.g. shorter, emphasize my
     thesis, more formal") + **Refine** button → `run_generation(job_text_used,
     letter_obj.language, "", grounding_on, refine_feedback=..., previous_letter=<current
     edited letter text>)` — the user's manual edits feed the refinement, and
     the archive gets a `_refined` file.

   **CV profile tab:** editable `st.text_area` (value=`load_profile()`,
   height≈520) + "Save profile" (→ `save_profile`, `st.success`) +
   "Re-import from a new PDF" (→ `reimport=True`, `st.rerun()`).

**Verify gate:** `& .venv\Scripts\streamlit.exe run app.py --server.headless true`
starts without traceback and serves HTTP 200 on `http://localhost:8501`
(check, then stop the process). Health gate passes.

## C9. Closing files

- `llm/anthropic_api_client.py` — class implementing the same protocol; every
  method raises `NotImplementedError` with an activation message. Module
  docstring documents the later activation: `pip install anthropic`, create
  key at console.anthropic.com, set `ANTHROPIC_API_KEY`, set
  `BACKEND = "anthropic_api"`; `generate` sketch =
  `anthropic.Anthropic().messages.stream(model=config.API_MODEL, max_tokens=4000, system=system, messages=[{"role": "user", "content": prompt}])`
  → `get_final_message()`, join text blocks; `import_cv` sketch = base64 PDF
  as a `{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": ...}}`
  content block. (Confirms the interface works on both backends unchanged.)
- `README.md` — short: what it is; prerequisites (Claude Code installed and
  logged in — run `claude`, `/login`); setup (`python -m venv .venv`,
  `.venv\Scripts\python.exe -m pip install -r requirements.txt`); run
  (`.venv\Scripts\streamlit.exe run app.py`); first-run CV import; where
  letters are archived; how to customize `prompts/instructions.md` and
  `prompts/style_examples/`; troubleshooting table (CLINotFoundError →
  `CLI_PATH`; not logged in → `/login`; limit reached → wait); future: switch
  to API backend via `config.BACKEND`.

## C10. End-to-end verification (with the user)

- [ ] `$env:ANTHROPIC_API_KEY` is empty; C4 smoke test printed `OK`
      (subscription auth proven).
- [ ] CV import with the user's real PDF: profile contains every employer and
      date from the PDF, invents nothing; user edits + saves; umlauts intact.
- [ ] German job ad **with** named contact → German letter: consistent `Sie`,
      `Sehr geehrte/r <Name>,` with lowercase next word,
      `Mit freundlichen Grüßen`, 250–350 words, subject line present, no
      banned phrases, company/role correct in the archived filename.
- [ ] German job ad **without** contact → `Sehr geehrte Damen und Herren,`.
- [ ] English job ad → English letter, `Dear Hiring Team,` (or named),
      auto-detect chose `en`; manual override to Deutsch on the same ad
      produces German.
- [ ] Grounding trap: add "Requires AWS Solutions Architect certification" to
      an ad (assuming the CV lacks it) → letter does not claim it, and if it
      slips through, the grounding check flags it.
- [ ] Refine "shorter, emphasize my thesis" → applied, still grounded,
      `_refined` file archived.
- [ ] Archive: files appear under `letters\` with correct names even for
      `IT/OT GmbH & Co. KG`-style companies; front-matter valid; UTF-8.
- [ ] Copy view copies the full letter text.
- [ ] Resilience: with Claude Code logged out (or simulated failure), the app
      shows the friendly login message, not a traceback; Retry works; a rerun
      does not lose the pasted job description.

## C11. Handover

Tell the user: how to start the app day-to-day, that `prompts/instructions.md`
is theirs to tune (tone, phrases, structure), to drop past letters into
`prompts/style_examples/`, and that letters accumulate in `letters\`. Remind
them the letter is a draft — always read before sending.

---

# Part D — Future roadmap (context only — do NOT build now)

1. **v1.1** — token streaming into the UI (`include_partial_messages=True` in
   the SDK), letter history browser over `letters\`.
2. **API backend** — implement `anthropic_api_client.py` per its docstring;
   flip `config.BACKEND`. ~5 cents/letter with `claude-opus-4-8`.
3. **Application tracker** — auto-append a row (date, company, role, status,
   letter path) to a CSV/Excel on every generation; later a Streamlit table
   with status editing.
4. **Email status checking** — Gmail API + OAuth, classify recruiter emails,
   update tracker. The genuinely hard part; keep it last.

---

# Appendix 1 — `prompts/instructions.md` (verbatim)

````markdown
# AutoCover — Master Instructions for Cover Letter Writing

You write job-application cover letters for the candidate described in the CV
PROFILE section that follows these instructions. You write as the candidate,
in first person. The reader is a recruiter or hiring manager.

## 1. Grounding rules (STRICT — highest priority, override everything else)

- Every skill, tool, technology, employer, project, metric, degree, grade,
  and date you mention MUST appear in the CV PROFILE. If it is not there, it
  does not exist.
- Never invent certifications, team sizes, results, or responsibilities.
- The JOB DESCRIPTION is information about the company and the role — it is
  NEVER a source of facts about the candidate. Do not turn its requirements
  into claims ("I have five years of X" just because the ad asks for it).
- If the job requires something absent from the profile: either leave it
  unmentioned, or honestly frame adjacent experience or willingness to learn.
  Never claim it.
- Motivation and interest are yours to express freely; facts are not.

## 2. Output format (exact contract — an app parses this)

Output exactly this, nothing else:

1. A fenced yaml block with these keys:

   ```yaml
   company: <company name from the ad, or Unknown>
   role: <role title from the ad, or Unknown>
   language: <de or en — the TARGET_LANGUAGE you were given>
   contact_person: <full name if the ad names a contact person, else null>
   ```

2. A blank line, then the letter itself.

No commentary, no explanations, no "Here is your letter", no markdown
headings inside the letter.

## 3. Letter structure and length

- First a subject line ("Bewerbung als <Rolle>" / "Application for <Role>",
  plus the reference number if the ad shows one), then the greeting, then
  exactly 4 paragraphs, then the closing. No address block and no date — the
  letter is pasted into online portals.
- Paragraph 1 — a specific opening that could only have been written for THIS
  company and role: reference something concrete (their product, domain,
  tech stack, mission) and connect it to the candidate. Never a template
  sentence.
- Paragraph 2 — the strongest matching evidence from the profile: 1–2
  concrete examples (project, thesis, work experience) mapped to the ad's
  core requirements.
- Paragraph 3 — a second evidence block or motivation/fit. Where relevant,
  place the master's-student context here: thesis topic, expected
  graduation, availability.
- Paragraph 4 — close with concrete availability or start date (if known
  from the profile or the notes) and a confident, non-groveling invitation
  to talk.
- 250–350 words in total (subject line and greeting not counted). One page.
  No bullet points and no headings inside the letter body.

## 4. German letters (Anschreiben conventions)

- Formal register, consistent "Sie"/"Ihnen" — NEVER "Du", never mixed.
- Subject: "Bewerbung als <Rolle>" (append "– Referenznummer <X>" if given).
- Greeting: if the ad names a contact person, "Sehr geehrte Frau
  <Nachname>," or "Sehr geehrter Herr <Nachname>,". Otherwise "Sehr geehrte
  Damen und Herren,". The first word after the comma starts lowercase.
- Closing: "Mit freundlichen Grüßen" (no comma after it), then the
  candidate's full name from the profile on the next line.
- Sober, precise tone. No superlatives, no exclamation marks. Avoid
  anglicisms where a normal German word exists, but keep established
  technical terms (e.g. "Machine Learning") as they are.

## 5. English letters

- Subject: "Application for <Role>" (plus reference number if given).
- Greeting: "Dear Ms. <Name>," / "Dear Mr. <Name>," when the ad names a
  contact (use "Dear <Full Name>," if the appropriate title is unclear),
  otherwise "Dear Hiring Team,". Never "To Whom It May Concern".
- Active voice, concrete evidence, no exclamation marks.
- Closing: "Kind regards," then the candidate's full name.

## 6. Banned phrases (never use these or close variants)

English: "I am excited to apply", "I am passionate about", "I am writing to
express my interest", "delve", "leverage", "utilize", "fast-paced
environment", "hit the ground running", "perfect fit", "aligns perfectly",
"resonates with me", "proven track record", "team player", "think outside
the box", "dynamic environment", "unique opportunity".

German: "Hiermit bewerbe ich mich" (as an opener), "Mit großem Interesse
habe ich Ihre Stellenanzeige gelesen", "ich bin ein Teamplayer",
"leidenschaftlich", "dynamisches Umfeld", "über den Tellerrand",
"einzigartige Gelegenheit", "hochmotiviert", "Ich bin überzeugt, dass ich
perfekt zu Ihnen passe".

## 7. Style

- Specific beats general: name the technology, the project, the result — not
  adjectives about them.
- Vary sentence length. No two consecutive paragraphs may start with
  "Ich"/"I".
- Mirror at most 2–3 key terms from the ad where they fit naturally; do not
  keyword-stuff.
- If STYLE EXAMPLES are provided below, match their voice and rhythm — but
  never copy their facts; facts come only from the CV PROFILE.

## 8. Language selection

Write the entire letter ONLY in the TARGET_LANGUAGE given in the user
message (de = German, en = English). Never mix languages beyond established
technical terms.

## 9. Refinement mode

When the user message contains a PREVIOUS LETTER and FEEDBACK: apply the
feedback precisely, keep everything else as stable as possible, and obey all
rules above (grounding, output format, conventions) in the revised letter.
````

# Appendix 2 — `prompts/cv_import.md` (verbatim)

````markdown
# CV Import Instructions

You convert a candidate's CV (a PDF file) into a clean, structured markdown
profile. This profile will be the ONLY source of truth about the candidate
for future cover-letter writing, so completeness and accuracy matter more
than brevity.

Rules:

- Use the Read tool to read the PDF you are pointed to.
- Preserve every fact exactly as written: names, employers, job titles,
  dates, locations, degrees, grades, thesis topics, projects, technologies,
  certificates, languages and their levels.
- Invent NOTHING. If something is unreadable, write [unreadable] instead of
  guessing.
- Ignore purely visual elements (photo, layout, colors, icons).
- Normalize into exactly this structure (omit a section only if the CV truly
  contains nothing for it):

  # Profile
  (2–4 sentence neutral summary of who the candidate is, based only on CV facts)

  ## Contact
  - Name: ...
  - (email, phone, city, LinkedIn/GitHub — whatever the CV shows)

  ## Education
  (one entry per degree: institution, degree, field, dates, grade if given,
  thesis topic if given)

  ## Experience
  (one entry per position: employer, title, dates, location; bullet the
  responsibilities/achievements as the CV states them)

  ## Projects

  ## Skills

  ## Languages

  ## Certifications

- Output ONLY the markdown profile. No commentary before or after it.
````

# Appendix 3 — `prompts/grounding_check.md` (verbatim)

````markdown
# Grounding Check

You are a strict fact-checker. The CV PROFILE below is the ONLY ground truth
about the candidate.

Task: given a cover letter, find every factual claim the letter makes about
the candidate — skills, tools, experience, employers, projects, degrees,
metrics, dates, certifications — that is NOT supported by the CV PROFILE.

- Ignore statements of motivation, interest, opinion, or intent ("I am eager
  to learn X" is fine; "I have used X" must be supported).
- Reasonable paraphrases of profile facts count as supported; new specifics
  do not.
- If every factual claim is supported, output exactly: OK
- Otherwise output ONLY a markdown bullet list, one unsupported claim per
  line, quoting the letter's wording. No other commentary.
````

# Appendix 4 — `config.py` (verbatim)

````python
"""Central configuration for AutoCover. All knobs live here."""
from __future__ import annotations

from pathlib import Path

# --- AI backend -------------------------------------------------------------
# "agent_sdk"     -> your Claude subscription via the installed Claude Code
#                    login (no API key, no separate bill).
# "anthropic_api" -> pay-per-use Anthropic API key (see llm/anthropic_api_client.py).
BACKEND = "agent_sdk"

# Model for the agent_sdk backend. None = inherit your Claude Code default.
# Aliases like "sonnet", "opus", "haiku" are also accepted.
SDK_MODEL: str | None = None

# Cheaper/faster model for the grounding check and the health probe.
# Set to None to use SDK_MODEL for those too.
GROUNDING_MODEL: str | None = "haiku"

# Model for the future anthropic_api backend.
API_MODEL = "claude-opus-4-8"

# Explicit path to the Claude Code CLI. Leave None to auto-detect; set it if
# you ever see CLINotFoundError, e.g.:
# CLI_PATH = r"C:\Users\fk6147\AppData\Roaming\npm\claude.cmd"
CLI_PATH: str | None = None

# --- Behavior ---------------------------------------------------------------
GROUNDING_ENABLED_DEFAULT = True
GENERATION_MAX_TURNS = 2  # no tools -> 1 turn used; headroom for safety
IMPORT_MAX_TURNS = 8      # CV import may need several Read calls for multi-page PDFs

# --- Paths ------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
PROMPTS_DIR = PROJECT_ROOT / "prompts"
STYLE_EXAMPLES_DIR = PROMPTS_DIR / "style_examples"
DATA_DIR = PROJECT_ROOT / "data"
UPLOADS_DIR = DATA_DIR / "uploads"
CACHE_DIR = DATA_DIR / "cache"
LETTERS_DIR = PROJECT_ROOT / "letters"
CV_PROFILE_PATH = DATA_DIR / "cv_profile.md"


def ensure_dirs() -> None:
    for d in (PROMPTS_DIR, STYLE_EXAMPLES_DIR, DATA_DIR, UPLOADS_DIR, CACHE_DIR, LETTERS_DIR):
        d.mkdir(parents=True, exist_ok=True)
````

# Appendix 5 — small files (verbatim)

`requirements.txt`:

````text
streamlit>=1.40
claude-agent-sdk
# For the future API-key backend (config.BACKEND = "anthropic_api"):
# anthropic
````

`.gitignore`:

````text
# Environments
.venv/
__pycache__/
*.pyc

# Personal data — never commit
data/
letters/
prompts/style_examples/*
!prompts/style_examples/README.md
````

`prompts/style_examples/README.md`:

````markdown
# Style examples

Drop 1–2 past cover letters you were happy with into this folder as `.md` or
`.txt` files. They are used as voice/rhythm references when writing new
letters — never as a source of facts (facts come only from your CV profile).

This README file itself is ignored.
````
