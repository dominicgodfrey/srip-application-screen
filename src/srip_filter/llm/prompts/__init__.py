"""Prompt templates for the LLM tasks (PRD §8).

One module per task, each exposing a ``SYSTEM`` string and a ``user_prompt(...)`` builder, so
business logic never inlines prompt text and the client stays task-agnostic.
"""
