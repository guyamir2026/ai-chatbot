"""Wrapper module for `memory/` package at repository root.

Re-exports the root-level memory package so callers can use
`from ai_chatbot.memory.extractor import extract_facts` consistent with
the rest of the ai_chatbot.* namespace.
"""

from memory import *  # noqa: F401,F403
