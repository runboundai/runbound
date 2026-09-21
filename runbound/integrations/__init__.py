"""Adapters for agent frameworks that own the call sites themselves.

Each module here is imported explicitly (``from runbound.integrations import
langchain``) and lazily binds its framework, so importing runbound never
requires a framework to be installed.
"""
