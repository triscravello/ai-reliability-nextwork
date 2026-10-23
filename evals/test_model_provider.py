"""Provider boundary: a slow Gemini call must surface as ModelProviderError.

A fake client replaces the SDK, so no API key and no network access are needed.
"""

import unittest

import httpx

from app.model_provider import GeminiModelProvider, ModelProviderError
from app.models import Issue


class _TimingOutModels:
    def generate_content(self, **_kwargs: object) -> None:
        raise httpx.ReadTimeout("Simulated slow provider.")


class _TimingOutClient:
    models = _TimingOutModels()


class GeminiProviderTimeoutTest(unittest.TestCase):
    def test_timeout_raises_provider_error(self) -> None:
        provider = GeminiModelProvider()
        provider._client = _TimingOutClient()  # type: ignore[assignment]
        issue = Issue(issue_id="TIMEOUT-001", title="Slow provider", body="Gemini never answers.")

        with self.assertRaises(ModelProviderError):
            provider.suggest(issue)


if __name__ == "__main__":
    unittest.main()
