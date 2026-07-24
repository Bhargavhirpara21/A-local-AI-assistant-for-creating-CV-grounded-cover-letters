"""Tests for backend-neutral result values and lazy client construction."""

from __future__ import annotations

import dataclasses
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from config import build_settings
from llm import get_client
from llm.base import HealthStatus, LLMClient, LLMResult


class _ProtocolClient:
    """Small concrete client used to prove runtime protocol compatibility."""

    def generate(
        self,
        system: str,
        prompt: str,
        *,
        model: str | None = None,
    ) -> LLMResult:
        return LLMResult(text=f"{system}:{prompt}:{model}")

    def import_cv(self, pdf_path: Path) -> LLMResult:
        return LLMResult(text=pdf_path.name)

    def research_job(
        self,
        system: str,
        prompt: str,
        *,
        model: str | None = None,
    ) -> LLMResult:
        return LLMResult(text=f"{system}:{prompt}:{model}")

    def health_check(self) -> HealthStatus:
        return HealthStatus(ok=True, detail="OK")


class LLMContractTests(unittest.TestCase):
    """Verify normalized values and protocol shape independently of any SDK."""

    def test_result_defaults_are_successful_and_immutable(self) -> None:
        """A minimal result should represent successful text and be frozen."""

        result = LLMResult(text="letter")

        self.assertFalse(result.is_error)
        self.assertIsNone(result.error_message)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.text = "changed"  # type: ignore[misc]

    def test_health_status_is_immutable(self) -> None:
        """Health state should be a stable value safe for session storage."""

        status = HealthStatus(ok=False, detail="login required")

        with self.assertRaises(dataclasses.FrozenInstanceError):
            status.ok = True  # type: ignore[misc]

    def test_concrete_client_satisfies_runtime_protocol(self) -> None:
        """A correctly shaped backend should satisfy the runtime protocol."""

        self.assertIsInstance(_ProtocolClient(), LLMClient)


class ClientFactoryTests(unittest.TestCase):
    """Verify backend selection imports and constructs only one adapter."""

    def setUp(self) -> None:
        """Create temporary settings for factory tests."""

        self._temporary_directory = tempfile.TemporaryDirectory()
        self.settings = build_settings(Path(self._temporary_directory.name))

    def tearDown(self) -> None:
        """Release temporary settings paths after each test."""

        self._temporary_directory.cleanup()

    def test_agent_sdk_branch_is_lazy_and_receives_settings(self) -> None:
        """Selecting Agent SDK should construct only its adapter."""

        captured: list[object] = []

        class FakeAgentClient:
            """Factory sentinel for the Agent SDK branch."""

            def __init__(self, settings: object) -> None:
                captured.append(settings)

        fake_module = types.ModuleType("llm.agent_sdk_client")
        fake_module.AgentSDKClient = FakeAgentClient  # type: ignore[attr-defined]
        previous_api_module = sys.modules.pop("llm.anthropic_api_client", None)
        try:
            with patch.dict(
                sys.modules,
                {"llm.agent_sdk_client": fake_module},
            ):
                client = get_client(self.settings)
                self.assertNotIn("llm.anthropic_api_client", sys.modules)
        finally:
            if previous_api_module is not None:
                sys.modules["llm.anthropic_api_client"] = previous_api_module

        self.assertIsInstance(client, FakeAgentClient)
        self.assertEqual(captured, [self.settings])

    def test_anthropic_branch_is_lazy_and_receives_settings(self) -> None:
        """Selecting API mode should not import the Agent SDK adapter."""

        captured: list[object] = []

        class FakeAPIClient:
            """Factory sentinel for the future API branch."""

            def __init__(self, settings: object) -> None:
                captured.append(settings)

        fake_module = types.ModuleType("llm.anthropic_api_client")
        fake_module.AnthropicAPIClient = FakeAPIClient  # type: ignore[attr-defined]
        api_settings = dataclasses.replace(
            self.settings,
            backend="anthropic_api",
        )
        previous_agent_module = sys.modules.pop("llm.agent_sdk_client", None)
        try:
            with patch.dict(
                sys.modules,
                {"llm.anthropic_api_client": fake_module},
            ):
                client = get_client(api_settings)
                self.assertNotIn("llm.agent_sdk_client", sys.modules)
        finally:
            if previous_agent_module is not None:
                sys.modules["llm.agent_sdk_client"] = previous_agent_module

        self.assertIsInstance(client, FakeAPIClient)
        self.assertEqual(captured, [api_settings])

    def test_invalid_backend_fails_before_importing_an_adapter(self) -> None:
        """Unknown configuration should raise one clear error immediately."""

        invalid = dataclasses.replace(self.settings, backend="unknown")

        with self.assertRaisesRegex(ValueError, "Unsupported AutoCover backend"):
            get_client(invalid)


if __name__ == "__main__":
    unittest.main()
