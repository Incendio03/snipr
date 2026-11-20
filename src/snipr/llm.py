"""LLM provider interface. The Anthropic provider is added in milestone 2."""

from __future__ import annotations


def get_provider(settings: dict):
    """Return the configured provider, or None when no API key is stored."""
    return None
