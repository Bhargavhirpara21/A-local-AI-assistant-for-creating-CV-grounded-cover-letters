"""Tests for Claude Agent SDK collection, options, and failure mapping."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from collections.abc import AsyncIterable, AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

from claude_agent_sdk import (
    AssistantMessage,
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
)

from config import Settings, build_settings
from llm.agent_sdk_client import AgentSDKClient, _collect
from llm.base import LLMResult


def _result(
    *,
    is_error: bool = False,
    result: str | None = None,
    errors: list[str] | None = None,
) -> ResultMessage:
    """Build one SDK terminal result with stable synthetic metadata."""

    return ResultMessage(
        subtype="success" if not is_error else "error",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="session-test",
        total_cost_usd=0.01,
        result=result,
        errors=errors,
    )


def _query_with(
    *messages: AssistantMessage | ResultMessage,
) -> object:
    """Return a query replacement that yields the supplied SDK messages."""

    async def fake_query(
        *,
        prompt: str | AsyncIterable[dict[str, Any]],
        options: ClaudeAgentOptions,
    ) -> AsyncIterator[AssistantMessage | ResultMessage]:
        del prompt, options
        for message in messages:
            yield message

    return fake_query


class CollectorTests(unittest.TestCase):
    """Verify normalization of the asynchronous SDK message stream."""

    def test_terminal_result_takes_precedence_over_assistant_text(self) -> None:
        """The SDK terminal result should be preferred when it contains text."""

        assistant = AssistantMessage(
            content=[TextBlock(text="partial")],
            model="test",
        )
        with patch(
            "llm.agent_sdk_client.query",
            _query_with(assistant, _result(result="final")),
        ):
            value = asyncio.run(_collect("prompt", ClaudeAgentOptions()))

        self.assertEqual(value.text, "final")
        self.assertEqual(value.cost_usd, 0.01)
        self.assertEqual(value.session_id, "session-test")

    def test_assistant_text_is_joined_when_terminal_text_is_empty(self) -> None:
        """Assistant blocks should provide the fallback successful text."""

        assistant = AssistantMessage(
            content=[TextBlock(text="one"), TextBlock(text="two")],
            model="test",
        )
        with patch(
            "llm.agent_sdk_client.query",
            _query_with(assistant, _result()),
        ):
            value = asyncio.run(_collect("prompt", ClaudeAgentOptions()))

        self.assertEqual(value.text, "one\ntwo")
        self.assertFalse(value.is_error)

    def test_terminal_error_preserves_message_and_adds_limit_hint(self) -> None:
        """Usage-limit errors should become actionable without raising."""

        with patch(
            "llm.agent_sdk_client.query",
            _query_with(_result(is_error=True, errors=["rate limit reached"])),
        ):
            value = asyncio.run(_collect("prompt", ClaudeAgentOptions()))

        self.assertTrue(value.is_error)
        self.assertIn("rate limit reached", value.error_message or "")
        self.assertIn("usage window", value.error_message or "")

    def test_empty_success_is_normalized_as_error(self) -> None:
        """A successful stream with no text must not masquerade as a letter."""

        with patch(
            "llm.agent_sdk_client.query",
            _query_with(_result()),
        ):
            value = asyncio.run(_collect("prompt", ClaudeAgentOptions()))

        self.assertTrue(value.is_error)
        self.assertEqual(value.error_message, "The model returned no text.")


class AgentSDKClientTests(unittest.TestCase):
    """Verify exact SDK permissions, prompt caching, health, and exceptions."""

    _temporary_directory: tempfile.TemporaryDirectory[str]
    root: Path
    settings: Settings
    client: AgentSDKClient

    def setUp(self) -> None:
        """Create isolated settings and prompt assets for each test."""

        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.settings = build_settings(self.root)
        self.settings.prompts_dir.mkdir(parents=True)
        (self.settings.prompts_dir / "cv_import.md").write_text(
            "# Import safely\n",
            encoding="utf-8",
        )
        self.client = AgentSDKClient(self.settings)

    def tearDown(self) -> None:
        """Release temporary client paths after each test."""

        self._temporary_directory.cleanup()

    def test_generate_caches_utf8_and_has_no_tools(self) -> None:
        """Generation should isolate settings and expose no tools."""

        expected = LLMResult(text="OK")
        with patch.object(self.client, "_run", return_value=expected) as run:
            actual = self.client.generate("Umlaut: Grüße", "prompt")

        self.assertEqual(actual, expected)
        cached = self.settings.system_prompt_cache_path.read_text(encoding="utf-8")
        self.assertEqual(cached, "Umlaut: Grüße")
        options = run.call_args.args[1]
        self.assertEqual(options.tools, [])
        self.assertEqual(options.allowed_tools, [])
        self.assertEqual(options.mcp_servers, {})
        self.assertTrue(options.strict_mcp_config)
        self.assertEqual(options.setting_sources, [])
        self.assertEqual(options.skills, [])
        self.assertEqual(options.plugins, [])
        self.assertIsNone(options.model)
        self.assertEqual(options.cwd, str(self.root))
        self.assertEqual(
            options.system_prompt,
            {
                "type": "file",
                "path": str(self.settings.system_prompt_cache_path.resolve()),
            },
        )

    def test_prompt_cache_failure_logs_omit_private_exception_details(self) -> None:
        """A cache error must not log a private path or prompt-derived detail."""

        secret = r"SECRET-PROMPT C:\private\cache\prompt.md"
        with self.assertLogs(
            "llm.agent_sdk_client",
            level=logging.WARNING,
        ) as captured:
            with patch.object(Path, "write_text", side_effect=OSError(secret)):
                result = self.client.generate("private system", "private prompt")

        self.assertTrue(result.is_error)
        self.assertNotIn(secret, "\n".join(captured.output))

    def test_cv_import_has_exact_path_guard_and_isolated_options(self) -> None:
        """CV import should expose Read only through an exact-file guard."""

        pdf_path = self.settings.cv_staging_dir / "attempt-1" / "cv.pdf"
        pdf_path.parent.mkdir(parents=True)
        pdf_path.write_bytes(b"%PDF-synthetic")

        with patch.object(
            self.client,
            "_run",
            return_value=LLMResult(text="# Profile"),
        ) as run:
            self.client.import_cv(pdf_path)

        prompt, options = run.call_args.args
        self.assertIn(str(pdf_path.resolve()), prompt)
        self.assertEqual(options.tools, ["Read"])
        self.assertEqual(options.allowed_tools, [])
        self.assertIsNotNone(options.can_use_tool)
        self.assertEqual(options.mcp_servers, {})
        self.assertTrue(options.strict_mcp_config)
        self.assertEqual(options.setting_sources, [])
        self.assertEqual(options.skills, [])
        self.assertEqual(options.plugins, [])
        self.assertEqual(options.cwd, str(pdf_path.parent.resolve()))
        self.assertEqual(
            options.max_buffer_size,
            self.settings.cv_import_max_buffer_bytes,
        )

    def test_cv_read_guard_allows_only_the_staged_pdf_identity(self) -> None:
        """The permission callback should deny siblings, traversal, and tools."""

        pdf_path = self.settings.cv_staging_dir / "attempt-1" / "cv.pdf"
        pdf_path.parent.mkdir(parents=True)
        pdf_path.write_bytes(b"%PDF-synthetic")
        sibling = self.settings.source_library_dir / "private.md"
        sibling.parent.mkdir(parents=True)
        sibling.write_text("private", encoding="utf-8")

        with patch.object(
            self.client,
            "_run",
            return_value=LLMResult(text="# Profile"),
        ) as run:
            self.client.import_cv(pdf_path)

        options = run.call_args.args[1]
        guard = options.can_use_tool
        self.assertIsNotNone(guard)
        assert guard is not None
        context = ToolPermissionContext()

        exact = asyncio.run(
            guard("Read", {"file_path": str(pdf_path.resolve())}, context)
        )
        relative = asyncio.run(
            guard("Read", {"file_path": "cv.pdf"}, context)
        )
        sibling_read = asyncio.run(
            guard("Read", {"file_path": str(sibling.resolve())}, context)
        )
        traversal_read = asyncio.run(
            guard(
                "Read",
                {
                    "file_path": str(
                        Path("..")
                        / ".."
                        / ".."
                        / "source_library"
                        / "private.md"
                    )
                },
                context,
            )
        )
        other_tool = asyncio.run(
            guard("Bash", {"command": "type cv.pdf"}, context)
        )
        invalid_input = asyncio.run(
            guard("Read", {"file_path": 123}, context)
        )

        self.assertIsInstance(exact, PermissionResultAllow)
        self.assertIsInstance(relative, PermissionResultAllow)
        for denied in (
            sibling_read,
            traversal_read,
            other_tool,
            invalid_input,
        ):
            self.assertIsInstance(denied, PermissionResultDeny)

    def test_run_uses_one_message_stream_only_for_guarded_cv_import(self) -> None:
        """A guarded request should reach the SDK as one streamed user message."""

        pdf_path = self.settings.cv_staging_dir / "attempt-1" / "cv.pdf"
        pdf_path.parent.mkdir(parents=True)
        pdf_path.write_bytes(b"%PDF-synthetic")
        captured_prompts: list[str | AsyncIterable[dict[str, Any]]] = []

        async def fake_collect(
            prompt: str | AsyncIterable[dict[str, Any]],
            options: ClaudeAgentOptions,
        ) -> LLMResult:
            del options
            captured_prompts.append(prompt)
            return LLMResult(text="# Profile")

        with patch("llm.agent_sdk_client._collect", side_effect=fake_collect):
            guarded = self.client.import_cv(pdf_path)

        self.assertFalse(guarded.is_error)
        self.assertEqual(len(captured_prompts), 1)
        streamed_prompt = captured_prompts[0]
        self.assertIsInstance(streamed_prompt, AsyncIterable)
        assert isinstance(streamed_prompt, AsyncIterable)

        async def consume_messages() -> list[dict[str, Any]]:
            return [message async for message in streamed_prompt]

        messages = asyncio.run(consume_messages())
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["type"], "user")
        self.assertEqual(messages[0]["message"]["role"], "user")
        self.assertIn(str(pdf_path.resolve()), messages[0]["message"]["content"])
        self.assertIsNone(messages[0]["parent_tool_use_id"])
        self.assertEqual(messages[0]["session_id"], "default")

    def test_run_keeps_tool_free_requests_as_plain_strings(self) -> None:
        """Generation should retain the existing one-shot string request shape."""

        captured_prompts: list[str | AsyncIterable[dict[str, Any]]] = []

        async def fake_collect(
            prompt: str | AsyncIterable[dict[str, Any]],
            options: ClaudeAgentOptions,
        ) -> LLMResult:
            del options
            captured_prompts.append(prompt)
            return LLMResult(text="OK")

        with patch("llm.agent_sdk_client._collect", side_effect=fake_collect):
            result = self.client.generate("system", "plain prompt")

        self.assertFalse(result.is_error)
        self.assertEqual(captured_prompts, ["plain prompt"])

    def test_research_has_only_bounded_web_tools(self) -> None:
        """Research should expose WebSearch/WebFetch without local-file tools."""

        with patch.object(
            self.client,
            "_run",
            return_value=LLMResult(text="research"),
        ) as run:
            self.client.research_job("Research rules", "official URL")

        options = run.call_args.args[1]
        self.assertEqual(options.tools, ["WebSearch", "WebFetch"])
        self.assertEqual(options.allowed_tools, ["WebSearch", "WebFetch"])
        self.assertNotIn("Read", options.tools)
        self.assertEqual(options.mcp_servers, {})
        self.assertTrue(options.strict_mcp_config)
        self.assertEqual(options.setting_sources, [])
        self.assertEqual(options.skills, [])

    def test_missing_pdf_and_prompt_are_actionable_results(self) -> None:
        """Expected local input failures should return errors instead of raising."""

        missing_pdf = self.client.import_cv(self.root / "missing.pdf")
        self.assertTrue(missing_pdf.is_error)
        self.assertIn("no longer exists", missing_pdf.error_message or "")

        pdf_path = self.root / "cv.pdf"
        pdf_path.write_bytes(b"%PDF")
        (self.settings.prompts_dir / "cv_import.md").unlink()
        missing_prompt = self.client.import_cv(pdf_path)
        self.assertTrue(missing_prompt.is_error)
        self.assertIn("instructions", missing_prompt.error_message or "")

    def test_run_maps_specific_and_unknown_exceptions(self) -> None:
        """Every SDK boundary failure should become a normalized safe result."""

        cases: tuple[tuple[BaseException, str], ...] = (
            (CLINotFoundError(), "not found"),
            (
                ProcessError(
                    "usage limit",
                    exit_code=1,
                    stderr="rate limit reached",
                ),
                "subscription limit",
            ),
            (
                CLIJSONDecodeError("bad", ValueError("invalid")),
                "unreadable",
            ),
            (CLIConnectionError("offline"), "communicate"),
            (ClaudeSDKError("401 unauthorized"), "not logged in"),
            (OSError("blocked"), "Windows"),
            (RuntimeError("unexpected"), "unexpected"),
        )
        for error, expected_fragment in cases:
            with self.subTest(error=type(error).__name__):

                def raise_from_run(coroutine: object) -> LLMResult:
                    close = getattr(coroutine, "close", None)
                    if callable(close):
                        close()
                    raise error

                with patch(
                    "llm.agent_sdk_client.asyncio.run",
                    side_effect=raise_from_run,
                ):
                    value = self.client._run("prompt", ClaudeAgentOptions())
                self.assertTrue(value.is_error)
                self.assertIn(expected_fragment, value.error_message or "")

    def test_unexpected_sdk_logs_never_expose_private_exception_text(self) -> None:
        """Generic boundary diagnostics must omit paths and provider content."""

        secret = r"SECRET-CV-CONTENT C:\private\staging\cv.pdf"
        for error in (OSError(secret), RuntimeError(secret)):
            with self.subTest(error=type(error).__name__):

                def raise_from_run(coroutine: object) -> LLMResult:
                    close = getattr(coroutine, "close", None)
                    if callable(close):
                        close()
                    raise error

                with self.assertLogs(
                    "llm.agent_sdk_client",
                    level=logging.WARNING,
                ) as captured:
                    with patch(
                        "llm.agent_sdk_client.asyncio.run",
                        side_effect=raise_from_run,
                    ):
                        value = self.client._run(
                            "private prompt",
                            ClaudeAgentOptions(),
                        )

                self.assertTrue(value.is_error)
                self.assertNotIn(secret, "\n".join(captured.output))

    def test_missing_cli_guidance_rejects_batch_shims(self) -> None:
        """Windows guidance should use the bundled/native executable path."""

        def raise_missing(coroutine: object) -> LLMResult:
            close = getattr(coroutine, "close", None)
            if callable(close):
                close()
            raise CLINotFoundError()

        with patch(
            "llm.agent_sdk_client.asyncio.run",
            side_effect=raise_missing,
        ):
            value = self.client._run("prompt", ClaudeAgentOptions())

        self.assertIn("native claude.exe", value.error_message or "")
        self.assertIn(".cmd and .bat", value.error_message or "")

    def test_health_check_classifies_success_auth_limit_and_unexpected_text(self) -> None:
        """Health guidance should distinguish actionable account conditions."""

        cases: tuple[tuple[LLMResult, bool, str], ...] = (
            (LLMResult(text="OK"), True, "ready"),
            (
                LLMResult(
                    text="",
                    is_error=True,
                    error_message="401 authentication failed",
                ),
                False,
                "not logged in",
            ),
            (
                LLMResult(
                    text="",
                    is_error=True,
                    error_message="rate limit",
                ),
                False,
                "subscription limit",
            ),
            (LLMResult(text="something else"), False, "unexpected"),
        )
        for result, expected_ok, expected_detail in cases:
            with self.subTest(result=result):
                with patch.object(self.client, "generate", return_value=result):
                    status = self.client.health_check()
                self.assertEqual(status.ok, expected_ok)
                self.assertIn(expected_detail, status.detail)


if __name__ == "__main__":
    unittest.main()
