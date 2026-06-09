"""Prompt templates for the LLM tasks (PRD §8).

One module per task. Each exposes a ``SYSTEM`` string and a ``user_prompt(...)`` builder so
business logic never inlines prompt text (CLAUDE.md). The LLM client itself is task-agnostic;
the per-task modules pass these in.
"""
