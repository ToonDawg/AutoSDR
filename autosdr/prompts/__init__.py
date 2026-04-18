"""Versioned prompt modules.

Each prompt module exports:
- ``PROMPT_VERSION``: string written to ``message.metadata`` so a regression in
  output quality can be attributed to a specific revision.
- ``build_messages`` (or ``SYSTEM_PROMPT`` + ``build_user_prompt``): pure
  functions that turn pipeline inputs into an LLM-ready system/user pair.
"""
