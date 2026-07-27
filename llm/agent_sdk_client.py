"""Claude Agent SDK backend using the user's logged-in Claude Code subscription."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterable, AsyncIterator
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    CanUseTool,
    ClaudeAgentOptions,
    ClaudeSDKError,
    CLIConnectionError,
    CLIJSONDecodeError,
    CLINotFoundError,
    PermissionResultAllow,
    PermissionResultDeny,
    ProcessError,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
    query,
)

from config import Settings, ensure_dirs
from llm.base import HealthStatus, LLMResult

LOGGER = logging.getLogger(__name__)


class AgentSDKClient:
    """Synchronous, failure-safe facade over the asynchronous Claude Agent SDK."""

    _settings: Settings

    def __init__(self, settings: Settings) -> None:
        """Initialize the backend with explicit immutable settings."""

        self._settings = settings

    def generate(
        self,
        system: str,
        prompt: str,
        *,
        model: str | None = None,
    ) -> LLMResult:
        """Generate tool-free text with an isolated file-form system prompt."""

        ensure_dirs(self._settings)
        cache_path = self._settings.system_prompt_cache_path
        try:
            cache_path.write_text(system, encoding="utf-8", newline="\n")
        except OSError as error:
            LOGGER.warning(
                "Could not cache the generation system prompt: %s",
                type(error).__name__,
            )
            return LLMResult(
                text="",
                is_error=True,
                error_message=(
                    "Could not prepare the private prompt cache. Check permissions "
                    "for the data/cache directory."
                ),
            )
        options = self._build_options(
            system_prompt={"type": "file", "path": str(cache_path.resolve())},
            tools=[],
            allowed_tools=[],
            model=model if model is not None else self._settings.sdk_model,
            max_turns=self._settings.generation_max_turns,
            cwd=self._settings.project_root,
        )
        return self._run(prompt, options)

    def import_cv(self, pdf_path: Path) -> LLMResult:
        """Read one CV PDF with the SDK Read tool and return Markdown."""

        if not pdf_path.is_file():
            return LLMResult(
                text="",
                is_error=True,
                error_message="The selected CV PDF no longer exists.",
            )
        prompt_path = self._settings.prompts_dir / "cv_import.md"
        try:
            system = prompt_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            LOGGER.warning("Could not read CV import instructions: %s", type(error).__name__)
            return LLMResult(
                text="",
                is_error=True,
                error_message=(
                    "CV import instructions are missing or unreadable. Reinstall "
                    "the application files and try again."
                ),
            )
        ensure_dirs(self._settings)
        resolved_pdf = pdf_path.resolve()
        options = self._build_options(
            system_prompt=system,
            tools=["Read"],
            allowed_tools=[],
            model=self._settings.sdk_model,
            max_turns=self._settings.import_max_turns,
            cwd=resolved_pdf.parent,
            can_use_tool=_build_pdf_read_guard(resolved_pdf),
            max_buffer_size=self._settings.cv_import_max_buffer_bytes,
        )
        prompt = (
            f"Read the CV PDF at {resolved_pdf} and convert it following "
            "your instructions. Output only the markdown profile."
        )
        return self._run(prompt, options)

    def research_job(
        self,
        system: str,
        prompt: str,
        *,
        model: str | None = None,
    ) -> LLMResult:
        """Research official vacancy sources with only web search/fetch tools."""

        ensure_dirs(self._settings)
        cache_path = self._settings.cache_dir / "last_research_system_prompt.md"
        try:
            cache_path.write_text(system, encoding="utf-8", newline="\n")
        except OSError as error:
            LOGGER.warning(
                "Could not cache the research system prompt: %s",
                type(error).__name__,
            )
            return LLMResult(
                text="",
                is_error=True,
                error_message=(
                    "Could not prepare the private research prompt cache. Check "
                    "permissions for the data/cache directory."
                ),
            )
        options = self._build_options(
            system_prompt={"type": "file", "path": str(cache_path.resolve())},
            tools=["WebSearch", "WebFetch"],
            allowed_tools=["WebSearch", "WebFetch"],
            model=model if model is not None else self._settings.research_model,
            max_turns=self._settings.research_max_turns,
            cwd=self._settings.project_root,
        )
        return self._run(prompt, options)

    def health_check(self) -> HealthStatus:
        """Verify subscription-backed generation and classify common failures."""

        result = self.generate(
            "You reply with exactly: OK",
            "ping",
            model=self._settings.grounding_model or self._settings.sdk_model,
        )
        if result.is_error:
            return HealthStatus(
                ok=False,
                detail=self._health_detail(result.error_message or ""),
            )
        if result.text.strip().rstrip(".").upper() != "OK":
            return HealthStatus(
                ok=False,
                detail=(
                    "Claude responded, but the health probe returned unexpected "
                    "text. Retry once before generating a letter."
                ),
            )
        return HealthStatus(ok=True, detail="Claude subscription connection is ready.")

    def _build_options(
        self,
        *,
        system_prompt: str | dict[str, str],
        tools: list[str],
        allowed_tools: list[str],
        model: str | None,
        max_turns: int,
        cwd: Path,
        can_use_tool: CanUseTool | None = None,
        max_buffer_size: int | None = None,
    ) -> ClaudeAgentOptions:
        cli_path = (
            str(self._settings.cli_path.resolve())
            if self._settings.cli_path is not None
            else None
        )
        return ClaudeAgentOptions(
            system_prompt=system_prompt,
            tools=tools,
            allowed_tools=allowed_tools,
            mcp_servers={},
            strict_mcp_config=True,
            setting_sources=[],
            skills=[],
            plugins=[],
            can_use_tool=can_use_tool,
            model=model,
            max_turns=max_turns,
            cwd=str(cwd.resolve()),
            cli_path=cli_path,
            max_buffer_size=max_buffer_size,
        )

    def _run(self, prompt: str, options: ClaudeAgentOptions) -> LLMResult:
        try:
            sdk_prompt: str | AsyncIterable[dict[str, Any]] = (
                _single_message_prompt(prompt)
                if options.can_use_tool is not None
                else prompt
            )
            return asyncio.run(_collect(sdk_prompt, options))
        except CLINotFoundError:
            LOGGER.warning("Claude Code CLI was not found")
            return LLMResult(
                text="",
                is_error=True,
                error_message=self._cli_missing_message(),
            )
        except ProcessError as error:
            LOGGER.warning(
                "Claude Code process failed with exit code %s",
                error.exit_code,
            )
            raw = " ".join(
                value
                for value in (str(error), getattr(error, "stderr", None))
                if value
            )
            return LLMResult(
                text="",
                is_error=True,
                error_message=self._friendly_failure(raw),
            )
        except CLIJSONDecodeError:
            LOGGER.warning("Claude Code returned unreadable SDK data")
            return LLMResult(
                text="",
                is_error=True,
                error_message=(
                    "Claude returned an unreadable response. Retry once; if it "
                    "continues, restart Claude Code and this app."
                ),
            )
        except CLIConnectionError:
            LOGGER.warning("Claude SDK connection failed")
            return LLMResult(
                text="",
                is_error=True,
                error_message=(
                    "Could not communicate with Claude Code. Close any stuck "
                    "Claude terminal process, then retry."
                ),
            )
        except ClaudeSDKError as error:
            LOGGER.warning("Claude SDK request failed: %s", type(error).__name__)
            return LLMResult(
                text="",
                is_error=True,
                error_message=self._friendly_failure(str(error)),
            )
        except OSError as error:
            LOGGER.warning(
                "Operating-system failure while running Claude: %s",
                type(error).__name__,
            )
            return LLMResult(
                text="",
                is_error=True,
                error_message=(
                    "Windows could not start or communicate with Claude Code. "
                    "Check the configured CLI path and retry."
                ),
            )
        except Exception as error:
            LOGGER.warning(
                "Unexpected failure at the Claude SDK boundary: %s",
                type(error).__name__,
            )
            return LLMResult(
                text="",
                is_error=True,
                error_message=(
                    "An unexpected Claude connection error occurred. Retry once; "
                    "no request was retried automatically."
                ),
            )

    def _cli_missing_message(self) -> str:
        configured = (
            f" The configured native executable was {self._settings.cli_path}."
            if self._settings.cli_path is not None
            else ""
        )
        return (
            "Claude Code was not found. Reinstall claude-agent-sdk so its bundled "
            "native claude.exe is restored. If cli_path is set explicitly, it "
            "must point to a native .exe; .cmd and .bat shims are unsupported."
            + configured
        )

    @staticmethod
    def _friendly_failure(raw_message: str) -> str:
        lowered = raw_message.casefold()
        if any(
            marker in lowered
            for marker in ("login", "credential", "authentication", "unauthorized", "401")
        ):
            return (
                "Claude Code is not logged in. Open a terminal, run claude, use "
                "/login, then restart this app."
            )
        if any(
            marker in lowered
            for marker in ("rate limit", "usage limit", "quota", "capacity", "exhausted")
        ):
            return (
                "Claude subscription limit reached. Try again after the usage "
                "window resets."
            )
        return (
            "Claude could not complete the request. Retry once; if it continues, "
            "open Claude Code in a terminal to check its account status."
        )

    def _health_detail(self, message: str) -> str:
        lowered = message.casefold()
        if "not found" in lowered or "cli path" in lowered:
            return self._cli_missing_message()
        return self._friendly_failure(message)


def _build_pdf_read_guard(pdf_path: Path) -> CanUseTool:
    """Build a permission callback allowing Read of exactly one PDF identity."""

    resolved_pdf = pdf_path.resolve()
    allowed_parent = resolved_pdf.parent

    async def guard(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        del context
        if tool_name != "Read":
            return PermissionResultDeny(
                message="CV import permits only reading the staged PDF."
            )
        raw_path = tool_input.get("file_path")
        if not isinstance(raw_path, str) or not raw_path:
            return PermissionResultDeny(
                message="CV import requires the staged PDF path."
            )
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = allowed_parent / candidate
        try:
            is_allowed = candidate.samefile(resolved_pdf)
        except (OSError, ValueError):
            is_allowed = False
        if not is_allowed:
            return PermissionResultDeny(
                message="CV import cannot read files other than the staged PDF."
            )
        normalized_input = dict(tool_input)
        normalized_input["file_path"] = str(resolved_pdf)
        return PermissionResultAllow(updated_input=normalized_input)

    return guard


async def _single_message_prompt(
    prompt: str,
) -> AsyncIterator[dict[str, Any]]:
    """Yield one SDK user message so guarded tool callbacks remain available."""

    yield {
        "type": "user",
        "message": {"role": "user", "content": prompt},
        "parent_tool_use_id": None,
        "session_id": "default",
    }


async def _collect(
    prompt: str | AsyncIterable[dict[str, Any]],
    options: ClaudeAgentOptions,
) -> LLMResult:
    texts: list[str] = []
    final: ResultMessage | None = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    texts.append(block.text)
        elif isinstance(message, ResultMessage):
            final = message

    joined = "\n".join(text for text in texts if text).strip()
    if final is not None and final.is_error:
        error_message = _result_error_message(final)
        return LLMResult(
            text="",
            is_error=True,
            error_message=error_message,
            cost_usd=final.total_cost_usd,
            session_id=final.session_id,
        )
    result_text = (
        final.result.strip()
        if final is not None
        and isinstance(final.result, str)
        and final.result.strip()
        else joined
    )
    if not result_text:
        return LLMResult(
            text="",
            is_error=True,
            error_message="The model returned no text.",
            cost_usd=final.total_cost_usd if final is not None else None,
            session_id=final.session_id if final is not None else None,
        )
    return LLMResult(
        text=result_text,
        cost_usd=final.total_cost_usd if final is not None else None,
        session_id=final.session_id if final is not None else None,
    )


def _result_error_message(final: ResultMessage) -> str:
    raw_errors: Any = getattr(final, "errors", None)
    if isinstance(raw_errors, list) and raw_errors:
        message = "; ".join(str(error) for error in raw_errors)
    elif isinstance(final.result, str) and final.result.strip():
        message = final.result.strip()
    else:
        message = str(getattr(final, "subtype", "Claude request failed."))
    lowered = message.casefold()
    if any(
        marker in lowered
        for marker in ("rate limit", "usage limit", "quota", "capacity", "exhausted")
    ):
        message += (
            " Claude subscription usage may be exhausted; try again after the "
            "usage window resets."
        )
    return message
