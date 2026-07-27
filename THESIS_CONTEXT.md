# AutoCover Product Context

## Problem

Preparing a high-quality application currently requires repeatedly combining a
CV, curated candidate facts, language-specific wording, and a vacancy, then
manually recording the application. That workflow is slow and makes it easy for
facts, wording, and tracking data to drift apart.

## Product outcome

AutoCover is a local Streamlit application that:

1. Creates a tailored German or English cover letter from the current vacancy.
2. Grounds candidate claims in a private, editable source library and imported
   CV reference.
3. Shows fit and verification information separately from the employer-facing
   letter.
4. Supports editing and AI-assisted refinement without discarding manual edits.
5. Archives each generated version.
6. Maintains one local Excel record per application.

## Users and operating context

The primary user is a master's student applying for full-time roles in Germany.
The application runs locally on Windows and uses the logged-in Claude Code
subscription through the Claude Agent SDK. It is a single-user product and
does not require hosting or an Anthropic API key. Storage and the user
interface are local, but Claude inference is remote: selected source text, the
current job posting, and explicit notes are sent to the configured Claude
backend only after the user clicks an import, research, generation, grounding,
or refinement action.

## Authoritative source library

The private library contains:

- `cover_letter_instructions.md`
- `bhargav_candidate_profile_en.md`
- `bhargav_candidate_profile_de.md`
- `master_cover_letter_en.md`
- `master_cover_letter_de.md`

For each generation, AutoCover reads the controller plus only the profile and
master library matching the target language. It does not embed their factual or
wording content in Python code and does not cache them across generations.
Edits therefore affect the next generated letter.

The source authority for candidate facts is:

1. A new explicit clarification recorded in the curated profile.
2. Verified official evidence incorporated into that profile.
3. The matching-language curated candidate profile.
4. The imported CV reference.
5. The master library as wording guidance only, never as a factual source.

Conflicts are surfaced for review. The application must not silently invent,
merge, or choose conflicting facts.

## Current v1 scope

- Local Streamlit interface.
- German and English language detection with manual override.
- Safe ZIP import and in-app editing of the five-file source library.
- PDF CV import and re-import.
- CV-grounded generation, optional official-source research, and a grounding
  check.
- Editable letter, copy view, refinement, and Markdown archive.
- Local `.xlsx` application tracker with editable status, applied date, and
  notes.

## Deferred scope

- DOCX/PDF export.
- Email inbox integration.
- Hosted or multi-user operation.
- Token streaming and a full document-history browser.

## Quality and privacy principles

- Truthfulness has priority over keyword matching.
- Every generated claim must be traceable to the current private sources.
- Personal data and application history never enter Git.
- Normal letter generation sends the reviewed Markdown reference, not the raw
  CV PDF, and never sends the Excel workbook or unrelated application history.
- Starting a new CV import blocks silent use of an older CV. If the new import
  is pending or fails, the previous confirmed CV may be used only after the
  user explicitly approves that fallback for the current application. The
  selected CV version and fallback decision are recorded with the result.
- The selected source-library version and the exact generation inputs are
  identified by separate content hashes in archives and tracker records.
  Exact prompts are retained only in a private, Git-ignored trace snapshot so
  an older output is auditable and its request can be replayed. Model
  nondeterminism means replay is not guaranteed to produce identical wording;
  the archived letter remains the authoritative historical output.
- A generated letter is always a draft that the user reviews before sending.
