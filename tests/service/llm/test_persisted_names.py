# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Names stored sessions carry, pinned while the LLM clients are restructured.

Message and content metadata keys, marker values, provider ids and error
class names outlive the code that wrote them: a session saved today must load
and replay after any refactor.  The wire goldens pin each key a client writes
and reads back in the same run; these pin the names themselves, including the
ones only old sessions still carry.

- Every key-shaped string literal under ``src/chrys/service/llm`` (a provider
  namespace or a ``_chrys_`` stamp) is listed exactly, wherever its module
  moves: renaming or dropping one fails here.  Request-local markers, which
  are removed before anything is sent or stored, are listed apart.
- Plain metadata keys and marker values are recorded by the goldens, which
  compare every ``additional_properties`` exactly; the ones no golden
  records must still appear as literals.
- Named constants keep their values at their import paths.

Change a list only together with what keeps older sessions loading.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

LLM_SOURCES = Path(__file__).resolve().parents[3] / "src" / "chrys" / "service" / "llm"
_KEY_SHAPED = re.compile(r"(?:openai|anthropic|deepseek)\.[A-Za-z0-9_.]+|_chrys_[a-z0-9_]+")

KEY_SHAPED_LITERALS = frozenset(
    {
        "_chrys_request_attempt_id",
        "anthropic.cache_creation_input_tokens",
        "anthropic.cache_read_input_tokens",
        "deepseek.prompt_cache_hit_tokens",
        "openai.cache_write_tokens",
        "openai.cached_input_tokens",
        "openai.reasoning_tokens",
        "openai.responses.replay_shadow",
        "openai.responses.shell.output_type",
    }
)

# Request-local markers a client sets and pops while it encodes one request:
# never sent or stored, so replacing them (with typed state, say) is free.
REQUEST_LOCAL_MARKERS = frozenset(
    {
        "_chrys_hosted_context_summary",
        "_chrys_pending_image_parts",
    }
)

# Stored names that are ordinary words and that no golden records by name, so
# only their presence is checked.  ``_attribution`` is read from old sessions
# and written by nothing; the goldens record its effects (the id a stored
# Responses call replays under, and whether reasoning beside a call survives
# the duplicate-id check).  Words too common to go missing (``error``,
# ``summary``) are not listed: a presence check could never fail for them.
UNRECORDED_PLAIN_LITERALS = frozenset({"_attribution", "anthropic_server_tool"})


def _string_literals() -> set[str]:
    """Every string constant in the LLM client sources, docstrings excepted."""
    literals: set[str] = set()
    for path in sorted(LLM_SOURCES.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        literals.update(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
        )
    return literals


def test_key_shaped_literals_are_exactly_the_recorded_set() -> None:
    key_shaped = {literal for literal in _string_literals() if _KEY_SHAPED.fullmatch(literal)}
    assert key_shaped - REQUEST_LOCAL_MARKERS == KEY_SHAPED_LITERALS


def test_unrecorded_plain_names_are_still_written_and_read() -> None:
    assert UNRECORDED_PLAIN_LITERALS - _string_literals() == set()


def test_named_persisted_constants_keep_their_values() -> None:
    from chrys.foundation.hosted_tools import (
        ANTHROPIC_HOSTED_WIRE_BLOCK_KEY,
        OPENAI_HOSTED_WIRE_ITEM_KEY,
        PRESENTATION_TEXT_SEGMENT_ID_KEY,
    )
    from chrys.kernel import OPENAI_OUTPUT_MESSAGE_ENVELOPE_KEY
    from chrys.service.llm.chat_completions.reasoning import (
        REASONING_CONTENT_FIELD,
        REASONING_DETAILS_FIELD,
        REASONING_FIELD,
        REASONING_FORMAT_KEY,
    )
    from chrys.service.llm.openai_responses.history import (
        OPENAI_SHELL_OUTPUT_TYPE_KEY,
        OPENAI_SHELL_OUTPUT_TYPE_LOCAL_SHELL_CALL,
        OPENAI_SHELL_OUTPUT_TYPE_SHELL_CALL,
    )
    from chrys.service.llm.openai_responses.hosted import OPENAI_HOSTED_REPLAY_SHADOW_KEY

    assert OPENAI_HOSTED_WIRE_ITEM_KEY == "openai.responses.hosted_item"
    assert ANTHROPIC_HOSTED_WIRE_BLOCK_KEY == "anthropic.hosted_block"
    assert PRESENTATION_TEXT_SEGMENT_ID_KEY == "_chrys_presentation_text_segment_id"
    assert OPENAI_OUTPUT_MESSAGE_ENVELOPE_KEY == "openai.responses.output_message_envelope"
    assert REASONING_FORMAT_KEY == "openai_reasoning_format"
    assert (REASONING_DETAILS_FIELD, REASONING_CONTENT_FIELD, REASONING_FIELD) == (
        "reasoning_details",
        "reasoning_content",
        "reasoning",
    )
    assert OPENAI_HOSTED_REPLAY_SHADOW_KEY == "openai.responses.replay_shadow"
    assert OPENAI_SHELL_OUTPUT_TYPE_KEY == "openai.responses.shell.output_type"
    assert (OPENAI_SHELL_OUTPUT_TYPE_SHELL_CALL, OPENAI_SHELL_OUTPUT_TYPE_LOCAL_SHELL_CALL) == (
        "shell_call_output",
        "local_shell_call_output",
    )


def test_provider_ids_and_error_class_names_stay() -> None:
    """Provider ids are stored in profiles; an error's class name is stored as its trajectory ``error_code``."""
    from chrys.service.llm.openai_exceptions import OpenAIContentFilterException
    from chrys.service.profiles.models.schema import VALID_PROVIDERS

    assert {"openai", "anthropic", "deepseek-openai", "glm-openai", "mock"} == VALID_PROVIDERS
    assert OpenAIContentFilterException.__name__ == "OpenAIContentFilterException"
