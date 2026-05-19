"""
LLM connections — the single place chat models are configured.

Primary backend: a local model served by Ollama (default: Gemma). Google,
Anthropic, and OpenAI work as drop-in fallbacks via the LLM_PROVIDER env var.
Provider SDKs are imported lazily, so you only need the package for the
provider you actually use.

No tools, no agentic retrieval here: the verifier classifies emails purely by
prompt, and the writer feeds context into its prompt from SQLite. So this
module is just a model factory — `get_llm(...)`.
"""

from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv
from langchain_core.language_models.chat_models import BaseChatModel

load_dotenv()

PROVIDERS = ("ollama", "google", "anthropic", "openai")

DEFAULT_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").lower()
# 0.0 = deterministic; the right default for the verifier's classification.
# The writer overrides this per call when it wants some variety.
DEFAULT_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0"))
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

# Per-provider default model. Override with the matching env var.
#   - Ollama: the `ollama pull` tag. "gemma4:e4b" is the small/fast Gemma —
#     fine for the verifier's sender triage. Bump OLLAMA_MODEL to "gemma4:26b"
#     (or similar) for heavier reasoning.
#   - Cloud: also needs the provider's API key env var (GOOGLE_API_KEY,
#     ANTHROPIC_API_KEY, OPENAI_API_KEY).
DEFAULT_MODELS: dict[str, str] = {
    "ollama": os.getenv("OLLAMA_MODEL", "gemma4:e4b"),
    "google": os.getenv("GOOGLE_MODEL", "gemini-2.0-flash"),
    "anthropic": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
    "openai": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
}


def get_llm(
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    **kwargs: Any,
) -> BaseChatModel:
    """
    Build a LangChain chat model.

    Args:
        provider: one of PROVIDERS. Defaults to the LLM_PROVIDER env var, then
            "ollama".
        model: model name / Ollama tag. Defaults to DEFAULT_MODELS[provider].
        temperature: sampling temperature. Defaults to LLM_TEMPERATURE (0.0).
        **kwargs: forwarded to the underlying ChatModel constructor (e.g.
            num_ctx for Ollama, max_tokens for cloud providers).

    Returns:
        A BaseChatModel — also a LangGraph-compatible Runnable.
    """
    provider = (provider or DEFAULT_PROVIDER).lower()
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown LLM provider {provider!r}; expected one of {PROVIDERS}.")
    model = model or DEFAULT_MODELS[provider]
    temperature = DEFAULT_TEMPERATURE if temperature is None else temperature

    if provider == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(model=model, temperature=temperature, base_url=OLLAMA_BASE_URL, **kwargs)
    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(model=model, temperature=temperature, **kwargs)
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model, temperature=temperature, **kwargs)
    # provider == "openai"
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=model, temperature=temperature, **kwargs)


if __name__ == "__main__":
    # Smoke test: build the default model and do a one-line round trip.
    # Needs Ollama running with the model pulled (`ollama pull gemma4:e4b`),
    # or LLM_PROVIDER set to a cloud provider with its API key in the env.
    llm = get_llm()
    print(f"{DEFAULT_PROVIDER} / {DEFAULT_MODELS[DEFAULT_PROVIDER]}")
    print("response:", llm.invoke("Reply with exactly the word: ok").content)
