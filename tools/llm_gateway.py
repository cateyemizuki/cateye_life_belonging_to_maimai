"""Compatibility import for the shared gateway.

Both import paths resolve to the same module so existing function/class patches,
exported helpers and accounting hooks continue to affect LLMService.
"""
import sys

from core import llm_gateway

sys.modules[__name__] = llm_gateway
