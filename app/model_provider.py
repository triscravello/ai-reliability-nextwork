import os
from typing import Protocol

import httpx
from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from app.models import Issue, ModelSuggestion

# Reuse the model identifier approved in Part 1.
MODEL_ID = "gemini-3.8-flash"

# Fail fast so a slow provider degrades to the baseline instead of hanging the caller.
PROVIDER_TIMEOUT_MS = 20_000

PROMPT_TEMPLATE = """You are a support triage assistant.
Suggest a priority (P0 most urgent to P3 least urgent), a routing team, and a one-sentence summary.

Title: {title}
Body: {body}
Customer impact: {customer_impact}"""


class ModelProviderError(Exception):
    """Raised when a provider cannot return a valid suggestion."""


# Vendor-neutral boundary: the orchestrator and tests depend only on this.
class ModelProvider(Protocol):
    def suggest(self, issue: Issue) -> ModelSuggestion: ...


class GeminiModelProvider:
    """Gemini-backed provider. All Gemini SDK code stays in this module."""

    def __init__(self, model_id: str = MODEL_ID) -> None:
        self._model_id = model_id
        self._client: genai.Client | None = None

    # Create the client on first use so imports never need a key or a network call.
    def _get_client(self) -> genai.Client:
        if self._client is None:
            load_dotenv()
            if not os.environ.get("GEMINI_API_KEY"):
                raise ModelProviderError("GEMINI_API_KEY is not configured.")
            self._client = genai.Client(http_options=types.HttpOptions(timeout=PROVIDER_TIMEOUT_MS))
        return self._client

    def suggest(self, issue: Issue) -> ModelSuggestion:
        prompt = PROMPT_TEMPLATE.format(
            title=issue.title,
            body=issue.body,
            customer_impact=issue.customer_impact or "not stated",
        )
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=ModelSuggestion.model_json_schema(),
        )
        try:
            response = self._get_client().models.generate_content(
                model=self._model_id,
                contents=prompt,
                config=config,
            )
            # Validate before anything model-generated leaves this boundary.
            return ModelSuggestion.model_validate_json(response.text or "")
        except (errors.APIError, ValidationError, httpx.TimeoutException) as error:
            raise ModelProviderError("Gemini did not return a valid suggestion.") from error
