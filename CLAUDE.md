# AutoCover

Local AI cover-letter generator (Streamlit + Claude Agent SDK on the user's
Claude subscription). The user is a master's student applying for jobs in
Germany; letters are German or English.

## The one rule

**`PLAN.md` is the authoritative implementation guide.** It was produced in a
dedicated planning session with the SDK surface and this machine's environment
already verified. Before implementing or changing anything, read `PLAN.md` and
follow it:

- Product decisions in its Part A are locked with the user — do not revisit.
- Build steps in its Part C run in order; each has a verify gate.
- Appendices contain verbatim file contents (prompts, config) — copy exactly.
- If the installed `claude-agent-sdk` differs from what Part B describes, use
  the fallback table in §B4 instead of redesigning.

## Conventions

- All file I/O with explicit `encoding="utf-8"` (German umlauts everywhere).
- No dependencies beyond `requirements.txt`; no features beyond `PLAN.md`
  (deferred features are deferred on purpose — see PLAN.md Part D).
- Never commit or expose `data/` (CV/personal data) or `letters/`.
- Smoke tests use `.venv\Scripts\python.exe` from the project root; keep
  `ANTHROPIC_API_KEY` unset so calls run on the subscription login.
