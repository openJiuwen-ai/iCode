# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Microsoft Agent Framework (MIT License; see NOTICE).

"""Chrys-owned chat/agent types."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from asyncio import iscoroutine
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
    Generator,
    Mapping,
    MutableMapping,
    Sequence,
)
from copy import deepcopy
from inspect import isawaitable
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Final,
    Generic,
    Literal,
    NewType,
    ReadOnly,
    TypeVar,
    cast,
    final,
    overload,
)

from pydantic import BaseModel
from typing_extensions import TypedDict

from ._content import (
    Content,
    UsageDetails,
    _combine_annotations,
    _combine_raw_representations,
    _ContentRun,
    _parse_content_list,
    _restore_compaction_annotation_in_additional_properties,
    _text_for_join,
    add_usage_details,
    normalize_stream_usage,
)
from ._serialization import SerializationMixin
from ._tool_expansion import _get_tool_expander
from .exceptions import AdditionItemMismatch, ContentError
from .identity import ContentList

if TYPE_CHECKING:
    from .tools import FunctionTool as _OwnedFunctionTool
else:
    _OwnedFunctionTool = object

type ToolTypes = _OwnedFunctionTool | Mapping[str, Any] | object

logger = logging.getLogger(__name__)

# Identity-only sentinel carried by synthetic stream-progress updates. It is
# deliberately opaque: rejected provider payloads must never ride heartbeat
# updates across response-validation boundaries.
# Keep this marker and the matching
# ``ChatResponseUpdate.transport_heartbeat`` / ``_process_update`` handling
# together when changing streaming types, so heartbeats remain distinguishable.
_STREAM_HEARTBEAT_MARKER = object()
RETRY_BOUNDARY_UPDATE_KEY = "_chrys_retry_boundary"


# region Content Parsing Utilities


# endregion

# region Constants and types


_UNSET_USAGE: Final[UsageDetails] = cast("UsageDetails", object())
"""Sentinel distinguishing an omitted ``latest_usage_details`` from an explicit None.

An omitted argument means "single-call response" and inherits ``usage_details``.
An explicit ``None`` means the final call's usage is genuinely unavailable and
must NOT fall back to the aggregate — consumers such as after-run
force-compression would otherwise mistake turn-total billing usage for the
final call's context occupancy.
"""


# region Content Class


# endregion


# region Chat Response constants

RoleLiteral = Literal["system", "user", "assistant", "tool"]
"""Literal type for known role values. Accepts any string for extensibility."""

Role = NewType("Role", str)
"""Type for chat message roles. Use string values directly (e.g., "user", "assistant").

Known values: "system", "user", "assistant", "tool"

Examples:
    .. code-block:: python

        from chrys.kernel._types import Message

        # Use string values directly
        user_msg = Message("user", ["Hello"])
        assistant_msg = Message("assistant", ["Hi there!"])

        # Custom roles are also supported
        custom_msg = Message("custom", ["Custom role message"])

        # Compare roles directly as strings
        if user_msg.role == "user":
            print("This is a user message")
"""

FinishReasonLiteral = Literal["stop", "length", "tool_calls", "content_filter"]
"""Literal type for known finish reason values. Accepts any string for extensibility."""

FinishReason = NewType("FinishReason", str)
"""Type for chat response finish reasons. Use string values directly.

Known values:
    - "stop": Normal completion
    - "length": Max tokens reached
    - "tool_calls": Tool calls triggered
    - "content_filter": Content filter triggered

Examples:
    .. code-block:: python

        from chrys.kernel._types import ChatResponse

        response = ChatResponse(messages=[...], finish_reason="stop")

        # Check finish reason directly as string
        if response.finish_reason == "stop":
            print("Response completed normally")
        elif response.finish_reason == "tool_calls":
            print("Tool calls need to be processed")
"""


# region Message


class Message(SerializationMixin):
    """Represents a chat message.

    Attributes:
        role: The role of the author of the message.
        contents: The chat message content items.
        author_name: The name of the author of the message.
        message_id: The ID of the chat message.
        additional_properties: Any additional properties associated with the chat message.
            Additional properties are used internally, they are not sent to services.
        raw_representation: The raw representation of the chat message from an underlying implementation.

    Examples:
        .. code-block:: python

            from chrys.kernel._content import Content
            from chrys.kernel._types import Message

            # Create a message with text content
            user_msg = Message("user", ["What's the weather?"])
            print(user_msg.text)  # "What's the weather?"

            # Create a system message
            system_msg = Message("system", ["You are a helpful assistant."])

            # Create a message with mixed content types
            assistant_msg = Message(
                "assistant",
                ["The weather is sunny!", Content.from_image_uri("https://...")],
            )
            print(assistant_msg.text)  # "The weather is sunny!"

            # Serialization - to_dict and from_dict
            msg_dict = user_msg.to_dict()
            # {'type': 'chat_message', 'role': 'user',
            #  'contents': [{'type': 'text', 'text': "What's the weather?"}], 'additional_properties': {}}
            restored_msg = Message.from_dict(msg_dict)
            print(restored_msg.text)  # "What's the weather?"

            # Serialization - to_json and from_json
            msg_json = user_msg.to_json()
            # '{"type": "chat_message", "role": "user", "contents": [...], ...}'
            restored_from_json = Message.from_json(msg_json)
            print(restored_from_json.role)  # "user"

    """

    DEFAULT_EXCLUDE: ClassVar[set[str]] = {"raw_representation"}

    def __init__(
        self,
        role: RoleLiteral | str,
        contents: Sequence[Content | str | Mapping[str, Any]] | None = None,
        *,
        author_name: str | None = None,
        message_id: str | None = None,
        additional_properties: MutableMapping[str, Any] | None = None,
        raw_representation: Any | None = None,
    ) -> None:
        """Initialize Message.

        Args:
            role: The role of the author of the message (e.g., "user", "assistant", "system", "tool").
            contents: A sequence of content items. Can be Content objects, strings (auto-converted
                to TextContent), or dicts (parsed via Content.from_dict). Defaults to empty list.

        Keyword Args:
            author_name: Optional name of the author of the message.
            message_id: Optional ID of the chat message.
            additional_properties: Optional additional properties associated with the chat message.
                Additional properties are used internally, they are not sent to services.
            raw_representation: Optional raw representation of the chat message.
        """
        parsed_contents = [] if contents is None else _parse_content_list(contents)

        self.role: str = role
        self.contents = parsed_contents
        self.author_name = author_name
        self.message_id = message_id
        self.additional_properties = (
            _restore_compaction_annotation_in_additional_properties(additional_properties) or {}
        )
        self.raw_representation = raw_representation
        # Runtime-only assembly provenance. The tool loop sets this when an
        # update for this logical message had echoed content removed before
        # assembly; private attributes are excluded from serialization.
        self._chrys_echo_content_stripped = False

    @property
    def contents(self) -> ContentList:
        """The chat message content items, always a ``ContentList``.

        The setter coerces any assigned sequence so captors can hold
        ``weakref.ref(msg.contents)`` (built-in ``list`` is not
        weakref-able). It backs onto ``__dict__["contents"]`` — the PUBLIC
        key — because ``to_dict`` enumerates ``__dict__`` dropping
        ``_``-prefixed keys and the generic ``__deepcopy__`` copies
        ``__dict__`` entries via ``object.__setattr__`` (which follows the
        descriptor protocol back into this setter). ``ContentList`` input is
        stored as-is, preserving list identity for shared-list twins and
        deepcopy re-coercion; a plain list is copied into a fresh
        ``ContentList``, so a caller's plain-list alias no longer aliases
        ``msg.contents`` after assignment.
        """
        return self.__dict__["contents"]

    @contents.setter
    def contents(self, value: Sequence[Content]) -> None:
        if not isinstance(value, ContentList):
            value = ContentList(value)
        self.__dict__["contents"] = value

    @property
    def text(self) -> str:
        """Returns the text content of the message.

        Remarks:
            This property concatenates the text of all TextContent objects in Content.
        """
        return " ".join(_text_for_join(content) for content in self.contents if content.type == "text")


AgentRunInputs = str | Content | Message | Sequence[str | Content | Message]


def normalize_messages(
    messages: AgentRunInputs | None = None,
) -> list[Message]:
    """Normalize message inputs to a list of Message objects.

    Args:
        messages: The input messages in various supported formats. Can be:
            - None (returns empty list)
            - A string (converted to a user message)
            - A Content object (wrapped in a user Message)
            - A Message object
            - A sequence containing any mix of the above

    Returns:
        A list of Message objects.
    """
    if messages is None:
        return []

    if isinstance(messages, str):
        return [Message("user", [messages])]

    if isinstance(messages, Content):
        return [Message("user", [messages])]

    if isinstance(messages, Message):
        return [messages]

    result: list[Message] = []
    for msg in messages:
        if isinstance(msg, (str, Content)):
            result.append(Message("user", [msg]))
        else:
            result.append(msg)
    return result


def prepend_instructions_to_messages(
    messages: list[Message],
    instructions: str | Sequence[str] | None,
    role: RoleLiteral | str = "system",
) -> list[Message]:
    """Prepend instructions to a list of messages with a specified role.

    This is a helper method for chat clients that need to add instructions
    from options as messages. Different providers support different roles for
    instructions (e.g., OpenAI uses "system", some providers might use "user").

    Args:
        messages: The existing list of Message objects.
        instructions: The instructions to prepend. Can be a single string or a sequence of strings.
        role: The role to use for the instruction messages. Defaults to "system".

    Returns:
        A new list with instruction messages prepended.

    Examples:
        .. code-block:: python

            from chrys.kernel._types import prepend_instructions_to_messages, Message

            messages = [Message("user", ["Hello"])]
            instructions = "You are a helpful assistant"

            # Prepend as system message (default)
            messages_with_instructions = prepend_instructions_to_messages(messages, instructions)

            # Or use a different role
            messages_with_user_instructions = prepend_instructions_to_messages(messages, instructions, role="user")
    """
    if instructions is None:
        return messages

    if isinstance(instructions, str):
        instructions = [instructions]

    # Skip instructions that are already present as leading messages with the
    # same role and text.  This prevents duplicate system messages when
    # instructions are injected by multiple layers (e.g. Agent + chat client).
    deduplicated: list[str] = []
    for idx, instr in enumerate(instructions):
        if idx < len(messages) and messages[idx].role == role and messages[idx].text == instr:
            continue
        deduplicated.append(instr)

    if not deduplicated:
        return messages

    instruction_messages = [Message(role, [instr]) for instr in deduplicated]
    return [*instruction_messages, *messages]


# region ChatResponse


def _is_transport_heartbeat_update(update: ChatResponseUpdate | AgentResponseUpdate) -> bool:
    """Return whether *update* reports stream progress without response semantics."""
    if isinstance(update, ChatResponseUpdate):
        return update.is_transport_heartbeat

    raw_update = update.raw_representation
    return (
        isinstance(raw_update, ChatResponseUpdate)
        and raw_update.is_transport_heartbeat
        and not update.contents
        and update.role is None
        and update.response_id is None
        and update.message_id is None
        and update.created_at is None
        and update.finish_reason is None
        and update.continuation_token is None
        and not update.additional_properties
    )


def is_retry_boundary_update(update: ChatResponseUpdate | AgentResponseUpdate) -> bool:
    """Return whether *update* marks a fresh wire-attempt boundary."""
    return (
        not update.contents
        and update.additional_properties is not None
        and update.additional_properties.get(RETRY_BOUNDARY_UPDATE_KEY) is True
    )


def _process_update(
    response: ChatResponse | AgentResponse,
    update: ChatResponseUpdate | AgentResponseUpdate,
    seen_contents: dict[int, Content],
) -> None:
    """Processes a single update and modifies the response in place.

    ``seen_contents`` is one assembly pass's identity memo (each
    ``from_updates``-style caller seeds an empty dict and threads it through
    every update): a content OBJECT the pass already incorporated is a
    duplicate emission, not a new fragment, and is skipped. Without this, a
    repeated function-call object adjacent to itself would SELF-MERGE —
    concatenating its own argument string into unparseable JSON — turning a
    valid call into an argument-parsing failure; the blocking path already
    collapses identity duplicates within one response to a single copy at
    landing, and assembly-scoped dedup gives streamed responses the same
    semantics (per attempt: each assembly seeds its own memo, so a retry
    re-emitting an object an earlier attempt streamed is unaffected). The memo maps id -> content precisely to RETAIN each object for
    the pass: generator-fed assembly holds no other reference to consumed
    fragments, and a merged-away fragment released mid-pass would let
    CPython hand a later fresh fragment the same id — silently dropping it.
    Usage contents are exempt: they never land in messages and their
    accumulation is value-based, not object-based.
    """
    if _is_transport_heartbeat_update(update) or is_retry_boundary_update(update):
        return
    is_new_message = False
    if (
        not response.messages
        or (
            update.message_id
            and response.messages[-1].message_id
            and response.messages[-1].message_id != update.message_id
        )
        or (update.role and response.messages[-1].role != update.role)
    ):
        is_new_message = True

    message = Message("assistant", []) if is_new_message else response.messages[-1]
    if isinstance(update, ChatResponseUpdate) and update._chrys_echo_content_stripped:
        # Preserve which logical message — not merely which model call —
        # lost echo content. The loop can then remove only a shell that
        # remained empty after its accepted stream was assembled.
        message._chrys_echo_content_stripped = True
    skipped_duplicate_content = False
    # Incorporate the update's properties into the message.
    if update.author_name is not None:
        message.author_name = update.author_name
    if update.role is not None:
        message.role = update.role
    if update.message_id:
        message.message_id = update.message_id
    for content in update.contents:
        # Fast path: get type attribute (most content will have it)
        content_type = getattr(content, "type", None)
        # Slow path: only check for dict if type is None
        if content_type is None and isinstance(content, (dict, MutableMapping)):
            try:
                content = Content.from_dict(dict(content.items()))
                content_type = content.type
            except ContentError as exc:
                logger.warning(f"Skipping unknown content type or invalid content: {exc}")
                continue
        if content_type != "usage":
            if id(content) in seen_contents:
                skipped_duplicate_content = True
                continue
            seen_contents[id(content)] = content
        match content_type:
            # mypy doesn't narrow type based on match/case, but we know these are FunctionCallContents
            case "function_call" if message.contents and message.contents[-1].type == "function_call":
                try:
                    message.contents[-1] += content  # type: ignore[operator]
                except AdditionItemMismatch, ContentError:
                    message.contents.append(content)
            case "usage":
                if response.usage_details is None:
                    response.usage_details = UsageDetails()
                # mypy doesn't narrow type based on match/case, but we know this is UsageContent
                response.usage_details = add_usage_details(response.usage_details, content.usage_details)  # type: ignore[arg-type]
                usage_details = content.usage_details or {}
                response.latest_usage_details = normalize_stream_usage(
                    [response.latest_usage_details or {}, usage_details]
                )
            case _:
                message.contents.append(content)
    if is_new_message and not (skipped_duplicate_content and not message.contents):
        # A duplicate object emitted under a fresh message boundary belongs
        # to the message that already incorporated it. Do not leave behind an
        # empty shell whose message_id/role makes response validation reject
        # an otherwise valid streamed response. Preserve the existing shape
        # for genuinely empty and usage-only updates.
        response.messages.append(message)
    # Incorporate the update's properties into the response.
    if update.response_id:
        response.response_id = update.response_id
    if update.created_at is not None:
        response.created_at = update.created_at
    if update.additional_properties is not None:
        response.additional_properties.update(update.additional_properties)
    if response.raw_representation is None:
        response.raw_representation = []
    if not isinstance(response.raw_representation, list):
        response.raw_representation = [response.raw_representation]
    raw_representation_value = cast(Any, response.raw_representation)
    raw_representation_list = cast(list[Any], raw_representation_value)
    raw_representation_list.append(update.raw_representation)
    if isinstance(response, ChatResponse) and isinstance(update, ChatResponseUpdate):
        if update.conversation_id is not None:
            response.conversation_id = update.conversation_id
        if update.finish_reason is not None:
            response.finish_reason = update.finish_reason
        if update.model is not None:
            response.model = update.model
    if (
        isinstance(response, AgentResponse)
        and isinstance(update, AgentResponseUpdate)
        and update.finish_reason is not None
    ):
        response.finish_reason = update.finish_reason
    response.continuation_token = update.continuation_token


def _coalesce_text_content(contents: list[Content], type_str: Literal["text", "text_reasoning"]) -> None:
    """Coalesce each run of adjacent ``type_str`` chunks that ``+`` would join into one content.

    A run is folded once (``_ContentRun``), so a stream of ``n`` chunks costs
    O(n) rather than the O(n²) of adding them one by one.
    """
    if not contents:
        return
    coalesced_contents: list[Content] = []
    run: _ContentRun | None = None
    for content in contents:
        if content.type != type_str:
            if run is not None:
                coalesced_contents.append(run.fold())
                run = None
            coalesced_contents.append(content)
        elif run is None:
            run = _ContentRun(content)
        elif run.accepts(content):
            run.add(content)
        else:
            # A new logical segment (another id, output message or format).
            coalesced_contents.append(run.fold())
            run = _ContentRun(content)
    if run is not None:
        coalesced_contents.append(run.fold())
    contents.clear()
    contents.extend(coalesced_contents)


def _content_items_text(items: Any) -> str | None:
    """Return concatenated text when a content item list only contains text."""
    if not isinstance(items, list):
        return None
    text_parts: list[str] = []
    content_items = cast(list[object], items)
    for item in content_items:
        if not isinstance(item, Content) or item.type != "text":
            return None
        text_parts.append(item.text or "")
    return "".join(text_parts)


def _merge_content_item_lists(existing: Any, incoming: Any) -> Any:
    """Merge streamed nested content lists, replacing deltas with a later full value when present."""
    if incoming is None:
        return existing
    if existing is None:
        return deepcopy(incoming)

    existing_text = _content_items_text(existing)
    incoming_text = _content_items_text(incoming)
    if existing_text is not None and incoming_text is not None:
        if incoming_text.startswith(existing_text):
            return deepcopy(incoming)
        if existing_text.startswith(incoming_text):
            return existing

        existing_items = cast(list[Content], existing)
        merged = deepcopy(existing_items[0])
        merged.text = existing_text + incoming_text
        return [merged]

    if isinstance(existing, list) and isinstance(incoming, list):
        existing_list = cast(list[object], existing)
        incoming_list = cast(list[object], incoming)
        return [*existing_list, *deepcopy(incoming_list)]
    return deepcopy(incoming)


def _merge_code_interpreter_content(existing: Content, incoming: Content) -> None:
    """Merge two code interpreter content items for the same logical call."""
    existing.inputs = _merge_content_item_lists(existing.inputs, incoming.inputs)
    existing.outputs = _merge_content_item_lists(existing.outputs, incoming.outputs)
    existing.annotations = _combine_annotations(existing.annotations, incoming.annotations)
    existing.additional_properties = {**existing.additional_properties, **incoming.additional_properties}
    existing.raw_representation = _combine_raw_representations(existing.raw_representation, incoming.raw_representation)


def _code_interpreter_key(content: Content) -> tuple[str, str] | None:
    """Return the aggregation key for code interpreter call/result content."""
    if content.type not in {"code_interpreter_tool_call", "code_interpreter_tool_result"}:
        return None
    call_id = content.call_id or content.additional_properties.get("item_id")
    if not isinstance(call_id, str) or not call_id:
        return None
    return content.type, call_id


def _coalesce_code_interpreter_content(contents: list[Content]) -> None:
    """Coalesce streaming code interpreter chunks by call id."""
    if not contents:
        return

    coalesced_contents: list[Content] = []
    seen: dict[tuple[str, str], Content] = {}
    for content in contents:
        key = _code_interpreter_key(content)
        if key is None:
            coalesced_contents.append(content)
            continue

        existing = seen.get(key)
        if existing is None:
            copied = deepcopy(content)
            seen[key] = copied
            coalesced_contents.append(copied)
            continue

        _merge_code_interpreter_content(existing, content)

    contents.clear()
    contents.extend(coalesced_contents)


def _finalize_response(response: ChatResponse | AgentResponse) -> None:
    """Finalizes the response by performing any necessary post-processing."""
    # A single response stream may carry cumulative usage snapshots.  Keep
    # only the latest per-key values; a surrounding tool loop explicitly
    # restores the aggregate across its distinct model calls afterwards.
    if response.latest_usage_details is not None:
        response.usage_details = response.latest_usage_details
    for msg in response.messages:
        _coalesce_text_content(msg.contents, "text")
        _coalesce_text_content(msg.contents, "text_reasoning")
        _coalesce_code_interpreter_content(msg.contents)


# region ContinuationToken


class ContinuationToken(TypedDict):
    """Opaque token for resuming long-running agent operations.

    A JSON-serializable dict used to poll for completion or resume a
    streaming response.  Presence on a response indicates the operation
    is still in progress; ``None`` means the operation is complete.

    Each provider subclasses this with its own fields; consumers should
    treat the token as opaque and simply pass it back to the same agent.

    Examples:
        .. code-block:: python

            import json

            # Persist token across restarts
            token_json = json.dumps(response.continuation_token)

            # Restore and resume
            token = json.loads(token_json)
            response = await agent.run(
                session=session,
                options={"continuation_token": token},
            )
    """


# endregion


def _parse_structured_response_value(text: str, response_format: Any | None) -> Any | None:
    if response_format is None:
        return None
    if not text:
        return None
    if isinstance(response_format, type) and issubclass(response_format, BaseModel):
        return response_format.model_validate_json(text)
    if isinstance(response_format, Mapping):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Response text is not valid JSON: {exc}") from exc
    logger.warning(
        "Unable to parse structured response value, use either a Pydantic model or a dict defining the schema, "
        "received response_format type: %s",
        type(response_format),  # type: ignore[reportUnknownArgumentType]
    )
    return None


def _last_non_empty_assistant_message_text(messages: Sequence[Message]) -> str:
    """Return structured-output text from the final substantive assistant message."""
    for message in reversed(messages):
        if message.role != "assistant":
            continue
        text = "".join((content.text or "") for content in message.contents if content.type == "text")
        if text.strip():
            return text
    return ""


_T = TypeVar("_T")
ChatResponseT = TypeVar("ChatResponseT", bound="ChatResponse")
ToolModeT = TypeVar("ToolModeT", bound="ToolMode")
AgentResponseT = TypeVar("AgentResponseT", bound="AgentResponse")
ResponseModelT = TypeVar("ResponseModelT", bound=BaseModel | None, default=None, covariant=True)
ResponseModelBoundT = TypeVar("ResponseModelBoundT", bound=BaseModel)
StructuredResponseFormat = type[BaseModel] | Mapping[str, Any] | None

CreatedAtT = str  # Use a datetimeoffset type? Or a more specific type like datetime.datetime?


class ChatResponse(SerializationMixin, Generic[ResponseModelT]):
    """Represents the response to a chat request.

    Attributes:
        messages: The list of chat messages in the response.
        response_id: The ID of the chat response.
        conversation_id: An identifier for the state of the conversation.
        model: The model used in the creation of the chat response.
        created_at: A timestamp for the chat response.
        finish_reason: The reason for the chat response.
        usage_details: Aggregate usage across all model calls represented by the response.
        latest_usage_details: Usage for the most recent model call only.
        structured_output: The structured output of the chat response, if applicable.
        additional_properties: Any additional properties associated with the chat response.
        raw_representation: The raw representation of the chat response from an underlying implementation.

    Note:
        The `author_name` attribute is available on the `Message` objects inside `messages`,
        not on the `ChatResponse` itself. Use `response.messages[0].author_name` to access
        the author name of individual messages.

    Examples:
        .. code-block:: python

            from chrys.kernel._types import ChatResponse, Message

            # Create a response with messages
            msg = Message("assistant", ["The weather is sunny."])
            response = ChatResponse(
                messages=[msg],
                finish_reason="stop",
                model="gpt-4",
            )
            print(response.text)  # "The weather is sunny."

            # Combine streaming updates
            updates = [...]  # List of ChatResponseUpdate objects
            response = ChatResponse.from_updates(updates)

            # Serialization - to_dict and from_dict
            response_dict = response.to_dict()
            # {'type': 'chat_response', 'messages': [...], 'model': 'gpt-4', 'finish_reason': 'stop'}
            restored_response = ChatResponse.from_dict(response_dict)
            print(restored_response.model)  # "gpt-4"

            # Serialization - to_json and from_json
            response_json = response.to_json()
            # '{"type": "chat_response", "messages": [...], "model": "gpt-4", ...}'
            restored_from_json = ChatResponse.from_json(response_json)
            print(restored_from_json.text)  # "The weather is sunny."
    """

    DEFAULT_EXCLUDE: ClassVar[set[str]] = {"raw_representation", "additional_properties"}
    _INTERNAL_CONVERSATION_ID_KEY: ClassVar[str] = "_chrys_internal_conversation_id"

    def __init__(
        self,
        *,
        messages: Message | Sequence[Message] | None = None,
        response_id: str | None = None,
        conversation_id: str | None = None,
        model: str | None = None,
        created_at: CreatedAtT | None = None,
        finish_reason: FinishReasonLiteral | FinishReason | None = None,
        usage_details: UsageDetails | None = None,
        latest_usage_details: UsageDetails | None = _UNSET_USAGE,
        value: ResponseModelT | None = None,
        response_format: StructuredResponseFormat = None,
        continuation_token: ContinuationToken | None = None,
        additional_properties: dict[str, Any] | None = None,
        raw_representation: Any | None = None,
    ) -> None:
        """Initializes a ChatResponse with the provided parameters.

        Keyword Args:
            messages: A single Message or sequence of Message objects to include in the response.
            response_id: Optional ID of the chat response.
            conversation_id: Optional identifier for the state of the conversation.
            model: Optional model used in the creation of the chat response.
            created_at: Optional timestamp for when the response was created.
            finish_reason: Optional reason for the chat response (e.g., "stop", "length", "tool_calls").
            usage_details: Optional aggregate usage for the chat response.
            latest_usage_details: Optional usage for the most recent model call.
                When omitted, defaults to ``usage_details`` (single-call response).
                Pass ``None`` explicitly when the final call's usage is unknown;
                it is preserved rather than falling back to the aggregate.
            value: Optional value of the structured output.
            response_format: Optional response format for the chat response.
            continuation_token: Optional token for resuming a long-running background operation.
                When present, indicates the operation is still in progress.
            additional_properties: Optional additional properties associated with the chat response.
            raw_representation: Optional raw representation of the chat response from an underlying implementation.
        """
        if messages is None:
            self.messages: list[Message] = []
        elif isinstance(messages, Message):
            self.messages = [messages]
        else:
            # Handle both Message objects and dicts (for from_dict support)
            processed_messages: list[Message] = []
            for msg in messages:
                if isinstance(msg, Message):
                    processed_messages.append(msg)
                elif isinstance(msg, dict):
                    processed_messages.append(Message.from_dict(msg))
                else:
                    processed_messages.append(msg)
            self.messages = processed_messages
        self.response_id = response_id
        self.conversation_id = conversation_id
        self.model = model
        self.created_at = created_at
        self.finish_reason = finish_reason
        self.usage_details = usage_details
        # ``usage_details`` may aggregate multiple tool-loop iterations for
        # billing; this value represents the final request's context occupancy.
        # Public attribute so serialization round-trips preserve both views.
        self.latest_usage_details: UsageDetails | None = (
            usage_details if latest_usage_details is _UNSET_USAGE else latest_usage_details
        )
        self._value: ResponseModelT | None = value
        self._response_format: StructuredResponseFormat = response_format
        self._value_parsed: bool = value is not None
        self.additional_properties = (
            _restore_compaction_annotation_in_additional_properties(additional_properties) or {}
        )
        self.continuation_token = continuation_token
        self.raw_representation: Any | list[Any] | None = raw_representation
        # Runtime-only marker set by the tool loop's exhaustion tail when it
        # stripped content that a service-stored conversation still holds:
        # continuation handles derived from this response must not be
        # propagated or restored. Private attributes are excluded from
        # serialization.
        self._chrys_service_state_invalidated = False

    def to_dict(self, *, exclude: set[str] | None = None, exclude_none: bool = True) -> dict[str, Any]:
        """Serialize, preserving an explicitly-unavailable latest usage.

        ``exclude_none`` would drop ``latest_usage_details=None`` and the
        constructor default would then substitute the billing aggregate on
        deserialization. ``None`` is a meaningful value here (final call's
        usage unknown), so it is kept as an explicit ``null`` whenever an
        aggregate exists to be wrongly inherited.
        """
        result = super().to_dict(exclude=exclude, exclude_none=exclude_none)
        if (
            exclude_none
            and self.latest_usage_details is None
            and "latest_usage_details" not in (exclude or ())
            and "usage_details" in result
        ):
            result["latest_usage_details"] = None
        return result

    def mark_internal_conversation_id(self) -> None:
        """Mark the current conversation_id as internal control-flow state."""
        self.additional_properties[self._INTERNAL_CONVERSATION_ID_KEY] = True

    def clear_internal_conversation_id(self) -> None:
        """Remove the internal conversation-id marker."""
        self.additional_properties.pop(self._INTERNAL_CONVERSATION_ID_KEY, None)

    def has_internal_conversation_id(self) -> bool:
        """Return whether conversation_id is internal control-flow state."""
        return bool(self.additional_properties.get(self._INTERNAL_CONVERSATION_ID_KEY, False))

    @overload
    @classmethod
    def from_updates(
        cls: type[ChatResponse[Any]],
        updates: Sequence[ChatResponseUpdate],
        *,
        output_format_type: type[ResponseModelBoundT],
    ) -> ChatResponse[ResponseModelBoundT]: ...

    @overload
    @classmethod
    def from_updates(
        cls: type[ChatResponse[Any]],
        updates: Sequence[ChatResponseUpdate],
        *,
        output_format_type: Mapping[str, Any],
    ) -> ChatResponse[Any]: ...

    @overload
    @classmethod
    def from_updates(
        cls: type[ChatResponse[Any]],
        updates: Sequence[ChatResponseUpdate],
        *,
        output_format_type: None = None,
    ) -> ChatResponse[Any]: ...

    @classmethod
    def from_updates(
        cls: type[ChatResponseT],
        updates: Sequence[ChatResponseUpdate],
        *,
        output_format_type: StructuredResponseFormat = None,
    ) -> ChatResponseT:
        """Joins multiple updates into a single ChatResponse.

        Example:
            .. code-block:: python

                from chrys.kernel._types import ChatResponse, ChatResponseUpdate

                # Create some response updates
                updates = [
                    ChatResponseUpdate(contents=[Content.from_text(text="Hello")], role="assistant"),
                    ChatResponseUpdate(contents=[Content.from_text(text=" How can I help you?")]),
                ]

                # Combine updates into a single ChatResponse
                response = ChatResponse.from_updates(updates)
                print(response.text)  # "Hello How can I help you?"

        Args:
            updates: A sequence of ChatResponseUpdate objects to combine.

        Keyword Args:
            output_format_type: Optional Pydantic model type or JSON schema mapping used to parse the
                response text into structured data.
        """
        msg = cls(messages=[], response_format=output_format_type)
        seen_contents: dict[int, Content] = {}
        for update in updates:
            _process_update(msg, update, seen_contents)
        _finalize_response(msg)
        return msg

    @overload
    @classmethod
    async def from_update_generator(
        cls: type[ChatResponse[Any]],
        updates: AsyncIterable[ChatResponseUpdate],
        *,
        output_format_type: type[ResponseModelBoundT],
    ) -> ChatResponse[ResponseModelBoundT]: ...

    @overload
    @classmethod
    async def from_update_generator(
        cls: type[ChatResponse[Any]],
        updates: AsyncIterable[ChatResponseUpdate],
        *,
        output_format_type: Mapping[str, Any],
    ) -> ChatResponse[Any]: ...

    @overload
    @classmethod
    async def from_update_generator(
        cls: type[ChatResponse[Any]],
        updates: AsyncIterable[ChatResponseUpdate],
        *,
        output_format_type: None = None,
    ) -> ChatResponse[Any]: ...

    @classmethod
    async def from_update_generator(
        cls: type[ChatResponseT],
        updates: AsyncIterable[ChatResponseUpdate],
        *,
        output_format_type: StructuredResponseFormat = None,
    ) -> ChatResponseT:
        """Joins multiple updates into a single ChatResponse.

        Example:
            .. code-block:: python

                from chrys.kernel._types import ChatResponse

                response = await ChatResponse.from_update_generator(
                    updates
                )
                print(response.text)

        Args:
            updates: An async iterable of ChatResponseUpdate objects to combine.

        Keyword Args:
            output_format_type: Optional Pydantic model type or JSON schema mapping used to parse the
                response text into structured data.
        """
        msg = cls(messages=[], response_format=output_format_type)
        seen_contents: dict[int, Content] = {}
        async for update in updates:
            _process_update(msg, update, seen_contents)
        _finalize_response(msg)
        return msg

    @property
    def text(self) -> str:
        """Returns the concatenated text of all messages in the response."""
        return self.raw_text.strip()

    @property
    def raw_text(self) -> str:
        """The concatenated message text without the outer-whitespace strip of ``text``.

        Format-sensitive consumers need the provider text verbatim: the
        LAST_WORDS structured-note validator treats leading indentation as
        CommonMark-meaningful, so a stripped view would silently legitimize
        a code-block-indented heading.
        """
        return "\n".join(message.text for message in self.messages if isinstance(message, Message))

    @property
    def value(self) -> ResponseModelT | None:
        """Get the parsed structured output value.

        If a response_format was provided and parsing hasn't been attempted yet,
        this parses the final non-empty assistant message into the specified type.

        Raises:
            ValidationError: If the response text doesn't match the expected schema.
            ValueError: If the response text is not valid JSON for a non-Pydantic structured format.
        """
        if self._value_parsed:
            return self._value
        if self._response_format is not None:
            self._value = cast(
                ResponseModelT,
                _parse_structured_response_value(
                    _last_non_empty_assistant_message_text(self.messages),
                    self._response_format,
                ),
            )
            self._value_parsed = True
        return self._value

    def __str__(self) -> str:
        return self.text


# region ChatResponseUpdate


class ChatResponseUpdate(SerializationMixin):
    """Represents a single streaming response chunk from a `ChatClient`.

    Attributes:
        contents: The chat response update content items.
        role: The role of the author of the response update.
        author_name: The name of the author of the response update. This is primarily used in
            multi-agent scenarios to identify which agent or participant generated the response.
            When updates are combined into a `ChatResponse`, the `author_name` is propagated
            to the resulting `Message` objects.
        response_id: The ID of the response of which this update is a part.
        message_id: The ID of the message of which this update is a part.
        conversation_id: An identifier for the state of the conversation of which this update is a part.
        model: The model associated with this response update.
        created_at: A timestamp for the chat response update.
        finish_reason: The finish reason for the operation.
        additional_properties: Any additional properties associated with the chat response update.
        raw_representation: The raw representation of the chat response update from an underlying implementation.

    Examples:
        .. code-block:: python

            from chrys.kernel._content import Content
            from chrys.kernel._types import ChatResponseUpdate

            # Create a response update with text content
            update = ChatResponseUpdate(
                contents=[Content.from_text(text="Hello")],
                role="assistant",
                message_id="msg_123",
            )
            print(update.text)  # "Hello"

            # Serialization - to_dict and from_dict
            update_dict = update.to_dict()
            # {'type': 'chat_response_update', 'contents': [{'type': 'text', 'text': 'Hello'}],
            #  'role': 'assistant', 'message_id': 'msg_123'}
            restored_update = ChatResponseUpdate.from_dict(update_dict)
            print(restored_update.text)  # "Hello"

            # Serialization - to_json and from_json
            update_json = update.to_json()
            # '{"type": "chat_response_update", "contents": [{"type": "text", "text": "Hello"}], ...}'
            restored_from_json = ChatResponseUpdate.from_json(update_json)
            print(restored_from_json.message_id)  # "msg_123"

    """

    DEFAULT_EXCLUDE: ClassVar[set[str]] = {"raw_representation"}

    def __init__(
        self,
        *,
        contents: Sequence[Content] | None = None,
        role: RoleLiteral | Role | None = None,
        author_name: str | None = None,
        response_id: str | None = None,
        message_id: str | None = None,
        conversation_id: str | None = None,
        model: str | None = None,
        created_at: CreatedAtT | None = None,
        finish_reason: FinishReasonLiteral | FinishReason | None = None,
        continuation_token: ContinuationToken | None = None,
        additional_properties: dict[str, Any] | None = None,
        raw_representation: Any | None = None,
    ) -> None:
        """Initializes a ChatResponseUpdate with the provided parameters.

        Keyword Args:
            contents: Optional list of Content items to include in the update.
            role: Optional role of the author of the response update (e.g., "user", "assistant").
            author_name: Optional name of the author of the response update.
            response_id: Optional ID of the response of which this update is a part.
            message_id: Optional ID of the message of which this update is a part.
            conversation_id: Optional identifier for the state of the conversation of which this update is a part
            model: Optional model associated with this response update.
            created_at: Optional timestamp for the chat response update.
            finish_reason: Optional finish reason for the operation.
            continuation_token: Optional token for resuming a long-running background operation.
                When present, indicates the operation is still in progress.
            additional_properties: Optional additional properties associated with the chat response update.
            raw_representation: Optional raw representation of the chat response update
                from an underlying implementation.

        """
        # Handle contents - support dict conversion for from_dict
        if contents is None:
            self.contents: list[Content] = []
        else:
            processed_contents: list[Content] = []
            for c in contents:
                if isinstance(c, Content):
                    processed_contents.append(c)
                elif isinstance(c, dict):
                    processed_contents.append(Content.from_dict(c))
                else:
                    processed_contents.append(c)
            self.contents = processed_contents

        self.role = role
        self.author_name = author_name
        self.response_id = response_id
        self.message_id = message_id
        self.conversation_id = conversation_id
        self.model = model
        self.created_at = created_at
        self.finish_reason = finish_reason
        self.continuation_token = continuation_token
        self.additional_properties = _restore_compaction_annotation_in_additional_properties(
            additional_properties,
            allow_none=True,
        )
        self.raw_representation = raw_representation
        # Runtime-only provenance copied through stream proxies and folded
        # into the corresponding Message by _process_update.
        self._chrys_echo_content_stripped = False

    @classmethod
    def transport_heartbeat(cls) -> ChatResponseUpdate:
        """Create an opaque progress update carrying no provider response data."""
        return cls(contents=[], raw_representation=_STREAM_HEARTBEAT_MARKER)

    @classmethod
    def retry_boundary(cls) -> ChatResponseUpdate:
        """Create an in-band marker for a fresh logical-call attempt."""
        return cls(contents=[], additional_properties={RETRY_BOUNDARY_UPDATE_KEY: True})

    @property
    def is_transport_heartbeat(self) -> bool:
        """Return whether this is the explicit opaque transport heartbeat."""
        return (
            self.raw_representation is _STREAM_HEARTBEAT_MARKER
            and not self.contents
            and self.role is None
            and self.author_name is None
            and self.response_id is None
            and self.message_id is None
            and self.conversation_id is None
            and self.model is None
            and self.created_at is None
            and self.finish_reason is None
            and self.continuation_token is None
            and not self.additional_properties
        )

    @property
    def text(self) -> str:
        """Returns the concatenated text of all contents in the update."""
        return "".join(_text_for_join(content) for content in self.contents if content.type == "text")

    def __str__(self) -> str:
        return self.text


# region AgentResponse


class AgentResponse(SerializationMixin, Generic[ResponseModelT]):
    """Represents the response to an Agent run request.

    Provides one or more response messages and metadata about the response.
    A typical response will contain a single message, but may contain multiple
    messages in scenarios involving function calls, RAG retrievals, or complex logic.

    Note:
        The `author_name` attribute is available on the `Message` objects inside `messages`,
        not on the `AgentResponse` itself. Use `response.messages[0].author_name` to access
        the author name of individual messages.

    Examples:
        .. code-block:: python

            from chrys.kernel._types import AgentResponse, Message

            # Create agent response
            msg = Message("assistant", ["Task completed successfully."])
            response = AgentResponse(messages=[msg], response_id="run_123")
            print(response.text)  # "Task completed successfully."

            # Combine streaming updates
            updates = [...]  # List of AgentResponseUpdate objects
            response = AgentResponse.from_updates(updates)

            # Serialization - to_dict and from_dict
            response_dict = response.to_dict()
            # {'type': 'agent_response', 'messages': [...], 'response_id': 'run_123',
            #  'additional_properties': {}}
            restored_response = AgentResponse.from_dict(response_dict)
            print(restored_response.response_id)  # "run_123"

            # Serialization - to_json and from_json
            response_json = response.to_json()
            # '{"type": "agent_response", "messages": [...], "response_id": "run_123", ...}'
            restored_from_json = AgentResponse.from_json(response_json)
            print(restored_from_json.text)  # "Task completed successfully."
    """

    DEFAULT_EXCLUDE: ClassVar[set[str]] = {"raw_representation"}

    def __init__(
        self,
        *,
        messages: Message | Sequence[Message] | None = None,
        response_id: str | None = None,
        agent_id: str | None = None,
        created_at: CreatedAtT | None = None,
        finish_reason: FinishReasonLiteral | FinishReason | None = None,
        usage_details: UsageDetails | None = None,
        latest_usage_details: UsageDetails | None = _UNSET_USAGE,
        value: ResponseModelT | None = None,
        response_format: StructuredResponseFormat = None,
        continuation_token: ContinuationToken | None = None,
        raw_representation: Any | None = None,
        additional_properties: dict[str, Any] | None = None,
    ) -> None:
        """Initialize an AgentResponse.

        Keyword Args:
            messages: A single Message or sequence of Message objects to include in the response.
            response_id: The ID of the chat response.
            agent_id: The identifier of the agent that produced this response. Useful in multi-agent
                scenarios to track which agent generated the response.
            created_at: A timestamp for the chat response.
            finish_reason: The reason the model stopped generating. Common values include
                ``"stop"`` (natural completion), ``"length"`` (token limit), and
                ``"tool_calls"`` (the model invoked a tool).
            usage_details: Aggregate usage across all model calls in the agent run.
            latest_usage_details: Usage for the most recent model call.
                When omitted, defaults to ``usage_details`` (single-call response).
                Pass ``None`` explicitly when the final call's usage is unknown;
                it is preserved rather than falling back to the aggregate.
            value: The structured output of the agent run response, if applicable.
            response_format: Optional response format for the agent response.
            continuation_token: Optional token for resuming a long-running background operation.
                When present, indicates the operation is still in progress.
            additional_properties: Any additional properties associated with the chat response.
            raw_representation: The raw representation of the chat response from an underlying implementation.
        """
        if messages is None:
            self.messages: list[Message] = []
        elif isinstance(messages, Message):
            self.messages = [messages]
        else:
            # Handle both Message objects and dicts (for from_dict support)
            processed_messages: list[Message] = []
            for msg in messages:
                if isinstance(msg, Message):
                    processed_messages.append(msg)
                elif isinstance(msg, dict):
                    processed_messages.append(Message.from_dict(msg))
                else:
                    processed_messages.append(msg)
            self.messages = processed_messages
        self.response_id = response_id
        self.agent_id = agent_id
        self.created_at = created_at
        self.finish_reason = finish_reason
        self.usage_details = usage_details
        # Public attribute so serialization round-trips preserve the
        # occupancy view alongside the billing aggregate.
        self.latest_usage_details: UsageDetails | None = (
            usage_details if latest_usage_details is _UNSET_USAGE else latest_usage_details
        )
        self._value: ResponseModelT | None = value
        self._response_format: type[BaseModel] | Mapping[str, Any] | None = response_format
        self._value_parsed: bool = value is not None
        self.additional_properties = (
            _restore_compaction_annotation_in_additional_properties(additional_properties) or {}
        )
        self.continuation_token = continuation_token
        self.raw_representation = raw_representation
        # Runtime-only mirror of the inner ChatResponse's invalidation marker
        # (see ChatResponse.__init__); the agent's streaming post-hook reads it
        # to skip restoring a service session handle the loop invalidated.
        self._chrys_service_state_invalidated = False

    @property
    def text(self) -> str:
        """Get the concatenated text of all messages."""
        return "".join(msg.text for msg in self.messages) if self.messages else ""

    @property
    def value(self) -> ResponseModelT | None:
        """Get the parsed structured output value.

        If a response_format was provided and parsing hasn't been attempted yet,
        this parses the final non-empty assistant message into the specified type.

        Raises:
            ValidationError: If the response text doesn't match the expected schema.
            ValueError: If the response text is not valid JSON for a non-Pydantic structured format.
        """
        if self._value_parsed:
            return self._value
        if self._response_format is not None:
            self._value = cast(
                ResponseModelT,
                _parse_structured_response_value(
                    _last_non_empty_assistant_message_text(self.messages),
                    self._response_format,
                ),
            )
            self._value_parsed = True
        return self._value

    def to_dict(self, *, exclude: set[str] | None = None, exclude_none: bool = True) -> dict[str, Any]:
        """Serialize, preserving an explicitly-unavailable latest usage.

        ``exclude_none`` would drop ``latest_usage_details=None`` and the
        constructor default would then substitute the billing aggregate on
        deserialization. ``None`` is a meaningful value here (final call's
        usage unknown), so it is kept as an explicit ``null`` whenever an
        aggregate exists to be wrongly inherited.
        """
        result = super().to_dict(exclude=exclude, exclude_none=exclude_none)
        if (
            exclude_none
            and self.latest_usage_details is None
            and "latest_usage_details" not in (exclude or ())
            and "usage_details" in result
        ):
            result["latest_usage_details"] = None
        return result

    @overload
    @classmethod
    def from_updates(
        cls: type[AgentResponse[Any]],
        updates: Sequence[AgentResponseUpdate],
        *,
        output_format_type: type[ResponseModelBoundT],
        value: Any | None = None,
    ) -> AgentResponse[ResponseModelBoundT]: ...

    @overload
    @classmethod
    def from_updates(
        cls: type[AgentResponse[Any]],
        updates: Sequence[AgentResponseUpdate],
        *,
        output_format_type: Mapping[str, Any],
        value: Any | None = None,
    ) -> AgentResponse[Any]: ...

    @overload
    @classmethod
    def from_updates(
        cls: type[AgentResponse[Any]],
        updates: Sequence[AgentResponseUpdate],
        *,
        output_format_type: None = None,
        value: Any | None = None,
    ) -> AgentResponse[Any]: ...

    @classmethod
    def from_updates(
        cls: type[AgentResponseT],
        updates: Sequence[AgentResponseUpdate],
        *,
        output_format_type: StructuredResponseFormat = None,
        value: Any | None = None,
    ) -> AgentResponseT:
        """Joins multiple updates into a single AgentResponse.

        Args:
            updates: A sequence of AgentResponseUpdate objects to combine.

        Keyword Args:
            output_format_type: Optional Pydantic model type or JSON schema mapping used to parse the
                response text into structured data.
            value: Optional pre-parsed structured output value to set directly on the response.
        """
        msg = cls(messages=[], response_format=output_format_type, value=value)
        seen_contents: dict[int, Content] = {}
        for update in updates:
            _process_update(msg, update, seen_contents)
        _finalize_response(msg)
        return msg

    @overload
    @classmethod
    async def from_update_generator(
        cls: type[AgentResponse[Any]],
        updates: AsyncIterable[AgentResponseUpdate],
        *,
        output_format_type: type[ResponseModelBoundT],
    ) -> AgentResponse[ResponseModelBoundT]: ...

    @overload
    @classmethod
    async def from_update_generator(
        cls: type[AgentResponse[Any]],
        updates: AsyncIterable[AgentResponseUpdate],
        *,
        output_format_type: Mapping[str, Any],
    ) -> AgentResponse[Any]: ...

    @overload
    @classmethod
    async def from_update_generator(
        cls: type[AgentResponse[Any]],
        updates: AsyncIterable[AgentResponseUpdate],
        *,
        output_format_type: None = None,
    ) -> AgentResponse[Any]: ...

    @classmethod
    async def from_update_generator(
        cls: type[AgentResponseT],
        updates: AsyncIterable[AgentResponseUpdate],
        *,
        output_format_type: StructuredResponseFormat = None,
    ) -> AgentResponseT:
        """Joins multiple updates into a single AgentResponse.

        Args:
            updates: An async iterable of AgentResponseUpdate objects to combine.

        Keyword Args:
            output_format_type: Optional Pydantic model type or JSON schema mapping used to parse the
                response text into structured data.
        """
        msg = cls(messages=[], response_format=output_format_type)
        seen_contents: dict[int, Content] = {}
        async for update in updates:
            _process_update(msg, update, seen_contents)
        _finalize_response(msg)
        return msg

    def __str__(self) -> str:
        return self.text


def _build_agent_response_from_chat_response(
    response: ChatResponse[Any],
    *,
    response_format: StructuredResponseFormat = None,
) -> AgentResponse[Any]:
    """Wrap a completed chat response without eagerly parsing structured output."""
    agent_response = AgentResponse(
        messages=response.messages,
        response_id=response.response_id,
        created_at=response.created_at,
        finish_reason=response.finish_reason,
        usage_details=response.usage_details,
        latest_usage_details=response.latest_usage_details,
        response_format=response_format,
        continuation_token=response.continuation_token,
        raw_representation=response,
        additional_properties=response.additional_properties,
    )
    if response._value_parsed:
        agent_response._value = response._value
        agent_response._value_parsed = True
    if response._chrys_service_state_invalidated:
        # Mirror the ephemeral invalidation marker so blocking callers see
        # the same no-continuation verdict the streaming finalizer stamps.
        agent_response._chrys_service_state_invalidated = True
    return agent_response


# region AgentResponseUpdate


class AgentResponseUpdate(SerializationMixin):
    """Represents a single streaming response chunk from an Agent.

    Attributes:
        contents: The content items in this update.
        role: The role of the author of the response update.
        author_name: The name of the author of the response update. In multi-agent scenarios,
            this identifies which agent generated this update. When updates are combined into
            an `AgentResponse`, the `author_name` is propagated to the resulting `Message` objects.
        agent_id: The identifier of the agent that produced this update. Useful in multi-agent
            scenarios to track which agent generated specific parts of the response.
        response_id: The ID of the response of which this update is a part.
        message_id: The ID of the message of which this update is a part.
        created_at: A timestamp for the response update.
        additional_properties: Any additional properties associated with the update.
        raw_representation: The raw representation from an underlying implementation.

    Examples:
        .. code-block:: python

            from chrys.kernel._content import Content
            from chrys.kernel._types import AgentResponseUpdate

            # Create an agent run update
            update = AgentResponseUpdate(
                contents=[Content.from_text(text="Processing...")],
                role="assistant",
                response_id="run_123",
            )
            print(update.text)  # "Processing..."

            # Serialization - to_dict and from_dict
            update_dict = update.to_dict()
            # {'type': 'agent_response_update', 'contents': [{'type': 'text', 'text': 'Processing...'}],
            #  'role': 'assistant', 'response_id': 'run_123'}
            restored_update = AgentResponseUpdate.from_dict(update_dict)
            print(restored_update.response_id)  # "run_123"

            # Serialization - to_json and from_json
            update_json = update.to_json()
            # '{"type": "agent_response_update", "contents": [{"type": "text", "text": "Processing..."}], ...}'
            restored_from_json = AgentResponseUpdate.from_json(update_json)
            print(restored_from_json.text)  # "Processing..."
    """

    DEFAULT_EXCLUDE: ClassVar[set[str]] = {"raw_representation"}

    def __init__(
        self,
        *,
        contents: Sequence[Content] | None = None,
        role: RoleLiteral | str | None = None,
        author_name: str | None = None,
        agent_id: str | None = None,
        response_id: str | None = None,
        message_id: str | None = None,
        created_at: CreatedAtT | None = None,
        finish_reason: FinishReasonLiteral | FinishReason | None = None,
        continuation_token: ContinuationToken | None = None,
        additional_properties: dict[str, Any] | None = None,
        raw_representation: Any | None = None,
    ) -> None:
        """Initialize an AgentResponseUpdate.

        Keyword Args:
            contents: Optional list of Content items to include in the update.
            role: The role of the author of the response update (e.g., "user", "assistant").
            author_name: Optional name of the author of the response update. Used in multi-agent
                scenarios to identify which agent generated this update.
            agent_id: Optional identifier of the agent that produced this update.
            response_id: Optional ID of the response of which this update is a part.
            message_id: Optional ID of the message of which this update is a part.
            created_at: Optional timestamp for the chat response update.
            finish_reason: The reason the model stopped generating. Common values include
                ``"stop"`` (natural completion), ``"length"`` (token limit), and
                ``"tool_calls"`` (the model invoked a tool).
            continuation_token: Optional token for resuming a long-running background operation.
                When present, indicates the operation is still in progress.
            additional_properties: Optional additional properties associated with the chat response update.
            raw_representation: Optional raw representation of the chat response update.

        """
        # Handle contents - support dict conversion for from_dict
        if contents is None:
            self.contents: list[Content] = []
        else:
            processed_contents: list[Content] = []
            for c in contents:
                if isinstance(c, Content):
                    processed_contents.append(c)
                elif isinstance(c, dict):
                    processed_contents.append(Content.from_dict(c))
                else:
                    processed_contents.append(c)
            self.contents = processed_contents

        self.role: str | None = role
        self.author_name = author_name
        self.agent_id = agent_id
        self.response_id = response_id
        self.message_id = message_id
        self.created_at = created_at
        self.finish_reason = finish_reason
        self.continuation_token = continuation_token
        self.additional_properties = _restore_compaction_annotation_in_additional_properties(
            additional_properties,
            allow_none=True,
        )
        self.raw_representation: Any | list[Any] | None = raw_representation

    @property
    def text(self) -> str:
        """Get the concatenated text of all TextContent objects in contents."""
        return "".join(_text_for_join(content) for content in self.contents if content.type == "text")

    def __str__(self) -> str:
        return self.text


# region ResponseStream


def map_chat_to_agent_update(update: ChatResponseUpdate, agent_name: str | None) -> AgentResponseUpdate:
    return AgentResponseUpdate(
        contents=update.contents,
        role=update.role,
        author_name=update.author_name or agent_name,
        response_id=update.response_id,
        message_id=update.message_id,
        created_at=update.created_at,
        finish_reason=update.finish_reason,  # type: ignore[arg-type]
        continuation_token=update.continuation_token,
        additional_properties=update.additional_properties,
        raw_representation=update,
    )


# Type variables for ResponseStream
def _raise_stream_teardown_errors(errors: Sequence[BaseException]) -> None:
    """Raise a collected teardown failure, preserving cancellation semantics."""
    for error in errors:
        if isinstance(error, asyncio.CancelledError):
            raise error
    raise errors[0]


async def _resolve_maybe_awaitable[T](value: T | Awaitable[T]) -> T:
    """Resolve a possibly-awaitable value without widening its result type."""
    if isawaitable(value):
        return cast("T", await value)
    return value


async def _resolve_stream_source[T](
    source: AsyncIterable[T] | Awaitable[AsyncIterable[T]],
) -> AsyncIterable[T]:
    """Resolve the two supported stream-source shapes without losing ``T``."""
    if isinstance(source, AsyncIterable):
        return cast("AsyncIterable[T]", source)
    if not iscoroutine(source):
        return cast("AsyncIterable[T]", source)
    return await source


@final
class ResponseStream[UpdateT, FinalT](AsyncIterable[UpdateT]):
    """Async stream wrapper that supports iteration and deferred finalization."""

    def __init__(
        self,
        stream: AsyncIterable[UpdateT] | Awaitable[AsyncIterable[UpdateT]],
        *,
        finalizer: Callable[[Sequence[UpdateT]], FinalT | Awaitable[FinalT]] | None = None,
        transform_hooks: list[Callable[[UpdateT], UpdateT | Awaitable[UpdateT | None] | None]] | None = None,
        cleanup_hooks: list[Callable[[], Awaitable[None] | None]] | None = None,
        result_hooks: list[Callable[[FinalT], FinalT | Awaitable[FinalT | None] | None]] | None = None,
    ) -> None:
        """A Async Iterable stream of updates.

        Args:
            stream: An async iterable or awaitable that resolves to an async iterable of updates.

        Keyword Args:
            finalizer: An optional callable that takes the list of all updates and produces a final result.
            transform_hooks: Optional list of callables that transform each update as it is yielded.
            cleanup_hooks: Optional list of callables that run after the stream is fully consumed (before finalizer).
            result_hooks: Optional list of callables that transform the final result (after finalizer).

        """
        self._stream_source = stream
        self._finalizer = finalizer
        self._stream: AsyncIterable[UpdateT] | None = None
        self._iterator: AsyncIterator[UpdateT] | None = None
        self._updates: list[UpdateT] = []
        self._consumed: bool = False
        self._finalized: bool = False
        self._final_result: FinalT | list[UpdateT] | None = None
        self._transform_hooks: list[Callable[[UpdateT], UpdateT | Awaitable[UpdateT | None] | None]] = (
            transform_hooks if transform_hooks is not None else []
        )
        self._update_filters: list[Callable[[UpdateT], UpdateT]] = []
        self._result_hooks: list[Callable[[FinalT], FinalT | Awaitable[FinalT | None] | None]] = (
            result_hooks if result_hooks is not None else []
        )
        self._cleanup_hooks: list[Callable[[], Awaitable[None] | None]] = (
            cleanup_hooks if cleanup_hooks is not None else []
        )
        self._cleanup_run: bool = False
        self._closed: bool = False
        self._abandoned: bool = False
        self._stream_error: Exception | None = None
        self._inner_stream: ResponseStream[Any, Any] | None = None
        self._inner_stream_source: ResponseStream[Any, Any] | Awaitable[ResponseStream[Any, Any]] | None = None
        self._wrap_inner: bool = False
        self._map_update: Callable[[Any], UpdateT | Awaitable[UpdateT]] | None = None
        self._pull_context_manager_factories: list[Callable[[], contextlib.AbstractContextManager[Any]]] = []

    @overload
    def map[OuterUpdateT, OuterFinalT](
        self,
        transform: Callable[[UpdateT], OuterUpdateT | Awaitable[OuterUpdateT]],
        finalizer: Callable[[Sequence[OuterUpdateT]], Awaitable[OuterFinalT]],
    ) -> ResponseStream[OuterUpdateT, OuterFinalT]: ...

    @overload
    def map[OuterUpdateT, OuterFinalT](
        self,
        transform: Callable[[UpdateT], OuterUpdateT | Awaitable[OuterUpdateT]],
        finalizer: Callable[[Sequence[OuterUpdateT]], OuterFinalT],
    ) -> ResponseStream[OuterUpdateT, OuterFinalT]: ...

    def map[OuterUpdateT, OuterFinalT](
        self,
        transform: Callable[[UpdateT], OuterUpdateT | Awaitable[OuterUpdateT]],
        finalizer: Callable[[Sequence[OuterUpdateT]], OuterFinalT | Awaitable[OuterFinalT]],
    ) -> ResponseStream[OuterUpdateT, OuterFinalT]:
        """Create a new stream that transforms each update.

        The returned stream delegates iteration to this stream, ensuring single consumption.
        Each update is transformed by the provided function before being yielded.

        Since the update type changes, a new finalizer MUST be provided that works with
        the transformed update type. The inner stream's finalizer cannot be used as it
        expects the original update type.

        When ``get_final_response()`` is called on the mapped stream:
        1. The inner stream's finalizer runs first (on the original updates)
        2. The inner stream's result_hooks run (on the inner final result)
        3. The outer stream's finalizer runs (on the transformed updates)
        4. The outer stream's result_hooks run (on the outer final result)

        This ensures that post-processing hooks registered on the inner stream (e.g.,
        context provider notifications, telemetry) are still executed.

        Args:
            transform: Function to transform each update to a new type.
            finalizer: Function to convert collected (transformed) updates to the final type.
                This is required because the inner stream's finalizer won't work with
                the new update type.

        Returns:
            A new ResponseStream with transformed update and final types.

        Example:
            >>> chat_stream.map(
            ...     lambda u: AgentResponseUpdate(...),
            ...     AgentResponse.from_updates,
            ... )
        """
        stream = ResponseStream[OuterUpdateT, OuterFinalT](self, finalizer=finalizer)
        stream._inner_stream_source = self
        stream._wrap_inner = True
        stream._map_update = transform
        return stream

    @overload
    def with_finalizer[OuterFinalT](
        self,
        finalizer: Callable[[Sequence[UpdateT]], Awaitable[OuterFinalT]],
    ) -> ResponseStream[UpdateT, OuterFinalT]: ...

    @overload
    def with_finalizer[OuterFinalT](
        self,
        finalizer: Callable[[Sequence[UpdateT]], OuterFinalT],
    ) -> ResponseStream[UpdateT, OuterFinalT]: ...

    def with_finalizer[OuterFinalT](
        self,
        finalizer: Callable[[Sequence[UpdateT]], OuterFinalT | Awaitable[OuterFinalT]],
    ) -> ResponseStream[UpdateT, OuterFinalT]:
        """Create a new stream with a different finalizer.

        The returned stream delegates iteration to this stream, ensuring single consumption.
        When `get_final_response()` is called, the inner stream is finalized first,
        including its result hooks, and retains its cached final response. The new
        finalizer then receives the collected updates to produce the outer response.
        Each stream caches its own result; repeated retrieval does not repeat either
        finalizer or its result hooks.

        Args:
            finalizer: Function to convert collected updates to the final response type.

        Returns:
            A new ResponseStream with the new final type.

        Example:
            >>> stream.with_finalizer(AgentResponse.from_updates)
        """
        stream = ResponseStream[UpdateT, OuterFinalT](self, finalizer=finalizer)
        stream._inner_stream_source = self
        stream._wrap_inner = True
        return stream

    @classmethod
    def from_awaitable(
        cls,
        awaitable: Awaitable[ResponseStream[UpdateT, FinalT]],
    ) -> ResponseStream[UpdateT, FinalT]:
        """Create a ResponseStream from an awaitable that resolves to a ResponseStream.

        This is useful when you have an async function that returns a ResponseStream
        and you want to wrap it to add hooks or use it in a pipeline.

        The returned stream delegates to the inner stream once it resolves, using the
        inner stream's finalizer if no new finalizer is provided.

        Args:
            awaitable: An awaitable that resolves to a ResponseStream.

        Returns:
            A new ResponseStream that wraps the awaitable.

        Example:
            >>> async def get_stream() -> ResponseStream[Update, Response]: ...
            >>> stream = ResponseStream.from_awaitable(get_stream())
        """
        stream: ResponseStream[UpdateT, FinalT] = cls(awaitable)
        stream._inner_stream_source = awaitable
        stream._wrap_inner = True
        return stream

    async def _get_stream(self) -> AsyncIterable[UpdateT]:
        if self._stream is None:
            self._stream = await _resolve_stream_source(self._stream_source)
            if self._map_update is None and isinstance(self._stream, ResponseStream):
                # Push registered update filters down to the source stream so
                # they run before ITS accumulation — the innermost finalizer
                # is what produces the final response. Mapped streams keep
                # filters local: theirs expect the post-map update type.
                for update_filter in self._update_filters:
                    self._stream.with_update_filter(update_filter)
            if isinstance(self._stream, ResponseStream) and self._wrap_inner:
                self._inner_stream = self._stream
                return self._inner_stream
        stream = self._stream
        if stream is None:
            raise RuntimeError("ResponseStream source did not resolve to an async iterable")
        return stream

    def __aiter__(self) -> ResponseStream[UpdateT, FinalT]:
        return self

    async def __anext__(self) -> UpdateT:
        if self._closed:
            raise StopAsyncIteration
        ended = False
        try:
            with contextlib.ExitStack() as stack:
                for factory in self._pull_context_manager_factories:
                    stack.enter_context(factory())
                # Resolve the underlying stream inside the pull contexts so that any
                # spans/contexts created during stream resolution (e.g. inner chat
                # completion spans created on the first pull of a wrapped agent stream)
                # inherit the active context (e.g. an outer agent invoke span).
                if self._iterator is None:
                    stream = await self._get_stream()
                    self._iterator = stream.__aiter__()
                update: UpdateT = await self._iterator.__anext__()
        except StopAsyncIteration:
            ended = True
        except asyncio.CancelledError:
            try:
                await self.aclose()
            except Exception:
                logger.debug("ResponseStream close failed while cancelling a pull", exc_info=True)
            raise
        except Exception as exc:
            self._stream_error = exc
            try:
                await self._run_cleanup_hooks()
            finally:
                self._stream_error = None
            raise
        if ended:
            # Finalized outside the handler: an error a hook raises is its own,
            # not one raised while the end of iteration was being handled.
            self._consumed = True
            await self._run_cleanup_hooks()
            await self.get_final_response()
            raise StopAsyncIteration
        if self._map_update is not None:
            update = await _resolve_maybe_awaitable(self._map_update(update))
        if self._update_filters and (self._map_update is not None or not isinstance(self._stream, ResponseStream)):
            # Filters apply exactly once, at the level that pulls raw updates
            # (or locally after a map transform); a ResponseStream source
            # already applied them via push-down before accumulating.
            for update_filter in self._update_filters:
                update = update_filter(update)
        self._updates.append(update)
        for hook in self._transform_hooks:
            hooked = await _resolve_maybe_awaitable(hook(update))
            if hooked is not None:
                update = hooked
        return update

    async def aclose(self) -> None:
        """Close this stream and every resolved inner stream without finalizing.

        Calling this method repeatedly is safe. An unresolved coroutine source
        is closed rather than awaited, so abandoning a lazy stream cannot start
        a provider request.
        """
        if self._closed:
            return
        self._closed = True
        # A close before finalization abandons the partial updates: cleanup
        # hooks (and anything else) must never finalize them into a
        # successful-looking response or fire result hooks on them.
        if not self._finalized:
            self._abandoned = True

        resolved: list[Any] = []
        if self._inner_stream is not None:
            resolved.append(self._inner_stream)
        if self._stream is not None:
            resolved.append(self._stream)
        if self._iterator is not None:
            resolved.append(self._iterator)

        seen: set[int] = set()
        close_errors: list[BaseException] = []
        try:
            for item in resolved:
                if item is self or id(item) in seen:
                    continue
                seen.add(id(item))
                try:
                    if isinstance(item, ResponseStream):
                        await item.aclose()
                        continue
                    close = getattr(item, "aclose", None)
                    if close is not None:
                        result = close()
                        if isawaitable(result):
                            await result
                except BaseException as exc:
                    close_errors.append(exc)

            if self._stream is None:
                sources = (self._stream_source, self._inner_stream_source)
                for source in sources:
                    if source is None or id(source) in seen:
                        continue
                    seen.add(id(source))
                    try:
                        if iscoroutine(source):
                            source.close()
                        elif isinstance(source, asyncio.Future) and not source.done():
                            source.cancel()
                        else:
                            close = getattr(source, "aclose", None)
                            if close is not None:
                                result = close()
                                if isawaitable(result):
                                    await result
                    except BaseException as exc:
                        close_errors.append(exc)
        finally:
            try:
                await self._run_cleanup_hooks()
            except BaseException as exc:
                close_errors.append(exc)
        if close_errors:
            _raise_stream_teardown_errors(close_errors)

    async def _resolve_stream_with_pull_contexts(self) -> AsyncIterable[UpdateT]:
        """Resolve the underlying stream while activating any registered pull context managers.

        Used by ``__await__`` and ``get_final_response`` so that any spans/contexts created
        during stream resolution (e.g. when the source is an Awaitable that internally
        creates child telemetry spans) inherit the same active context as iterator pulls.
        ``__anext__`` resolves the stream inside its own ExitStack and so calls ``_get_stream``
        directly.
        """
        if self._stream is not None:
            return await self._get_stream()
        with contextlib.ExitStack() as stack:
            for factory in self._pull_context_manager_factories:
                stack.enter_context(factory())
            return await self._get_stream()

    def __await__(self) -> Generator[Any, None, ResponseStream[UpdateT, FinalT]]:
        async def _wrap() -> ResponseStream[UpdateT, FinalT]:
            await self._resolve_stream_with_pull_contexts()
            return self

        return _wrap().__await__()

    async def get_final_response(self) -> FinalT:
        """Get the final response by applying the finalizer to all collected updates.

        If a finalizer is configured, it receives the list of updates and returns the final type.
        Result hooks are then applied in order to transform the result.

        If no finalizer is configured, returns the collected updates as Sequence[UpdateT].

        For wrapped streams (created via .map() or .from_awaitable()):
        - The inner stream's finalizer is called first to produce the inner final result.
        - The inner stream's result_hooks are then applied to that inner result.
        - The outer stream's finalizer is called to convert the outer (mapped) updates to the final type.
        - The outer stream's result_hooks are then applied to transform the outer result.

        This ensures that post-processing hooks registered on the inner stream (e.g., context
        provider notifications) are still executed even when the stream is wrapped/mapped.
        """
        if self._abandoned and not self._finalized:
            raise RuntimeError("ResponseStream was closed before completion; no final response is available.")
        if self._wrap_inner:
            if self._inner_stream is None:
                # Use _resolve_stream_with_pull_contexts() so that any spans/contexts
                # created while resolving the awaitable (e.g. inner telemetry spans)
                # inherit the same active context as iterator pulls. This also handles
                # the case where _stream_source and _inner_stream_source are the same
                # coroutine (e.g., from from_awaitable), avoiding double-await errors.
                await self._resolve_stream_with_pull_contexts()
            if self._inner_stream is None:
                raise RuntimeError("Inner stream not available")
            if not self._finalized and not self._consumed:
                # Consume outer stream (which delegates to inner) if not already consumed
                async for _ in self:
                    pass

            # Re-check: __anext__ auto-finalization may have already finalized this stream
            if not self._finalized:
                # This ensures inner post-processing (e.g., context provider notifications) runs
                # Skip if inner stream was already finalized (e.g., via auto-finalization on iteration)
                if not self._inner_stream._finalized:
                    inner_stream = self._inner_stream
                    inner_result: Any
                    if inner_stream._finalizer is not None:
                        inner_finalizer = inner_stream._finalizer
                        inner_result = await _resolve_maybe_awaitable(inner_finalizer(inner_stream._updates))
                    else:
                        inner_result = list(inner_stream._updates)

                    # Run inner stream's result hooks
                    inner_hooks = cast(list[Callable[[Any], Any | Awaitable[Any] | None]], inner_stream._result_hooks)
                    for hook in inner_hooks:
                        hooked_result = await _resolve_maybe_awaitable(hook(inner_result))
                        if hooked_result is not None:
                            inner_result = hooked_result
                    inner_stream._final_result = inner_result
                    inner_stream._finalized = True
                else:
                    inner_result = self._inner_stream._final_result

                # Now finalize the outer stream with its own finalizer
                # If outer has no finalizer, use inner's result (preserves from_awaitable behavior)
                outer_result: Any
                if self._finalizer is not None:
                    outer_result = await _resolve_maybe_awaitable(self._finalizer(self._updates))
                else:
                    # No outer finalizer - use inner's finalized result
                    outer_result = inner_result

                # Apply outer's result_hooks
                outer_hooks = cast(list[Callable[[Any], Any | Awaitable[Any] | None]], self._result_hooks)
                for hook in outer_hooks:
                    outer_hook_result = await _resolve_maybe_awaitable(hook(outer_result))
                    if outer_hook_result is not None:
                        outer_result = outer_hook_result
                self._final_result = outer_result
                self._finalized = True
            return cast("FinalT", self._final_result)

        if not self._finalized and not self._consumed:
            async for _ in self:
                pass

        # Re-check: __anext__ auto-finalization may have already finalized this stream
        if not self._finalized:
            result: Any
            if self._finalizer is not None:
                result = await _resolve_maybe_awaitable(self._finalizer(self._updates))
            else:
                result = list(self._updates)

            final_hooks = cast(list[Callable[[Any], Any | Awaitable[Any] | None]], self._result_hooks)
            for hook in final_hooks:
                final_hook_result = await _resolve_maybe_awaitable(hook(result))
                if final_hook_result is not None:
                    result = final_hook_result
            self._final_result = result
            self._finalized = True
        return cast("FinalT", self._final_result)

    def with_transform_hook(
        self,
        hook: Callable[[UpdateT], UpdateT | Awaitable[UpdateT | None] | None],
    ) -> ResponseStream[UpdateT, FinalT]:
        """Register a transform hook executed for each update during iteration."""
        self._transform_hooks.append(hook)
        return self

    def with_update_filter(
        self,
        update_filter: Callable[[UpdateT], UpdateT],
    ) -> ResponseStream[UpdateT, FinalT]:
        """Register a synchronous filter applied to each update BEFORE accumulation.

        Transform hooks run after an update joins ``updates`` and shape only
        what consumers iterate; an update filter runs before accumulation, so
        the finalizer's assembly, every result hook, transform hooks, and
        iteration all observe the same filtered sequence. Wrapped streams
        push filters down to the innermost stream — whose finalizer produces
        the final response — and apply them exactly once, at the level that
        pulls from the raw update source (mapped streams apply theirs
        locally, after the map transform, matching the mapped update type).
        The filter must return an update; return a modified copy rather than
        mutating the argument, so producer-held aliases keep their shape.

        Push-down follows ``ResponseStream`` sources only. A stream whose
        source is a plain async generator that itself drains ANOTHER stream
        and replays its updates (a semantic proxy, e.g. response-validation
        middleware) is a boundary push-down cannot cross — a filter
        registered on the proxy shapes only the replayed updates, never the
        drained stream's assembly. To reach beneath such proxies, deliver
        the filter on the request path instead
        (``ChatContext.stream_update_filters`` — the middleware pipeline
        attaches it to the stream its final handler resolves).
        """
        self._update_filters.append(update_filter)
        if self._map_update is None and isinstance(self._stream, ResponseStream):
            self._stream.with_update_filter(update_filter)
        return self

    def with_result_hook(
        self,
        hook: Callable[[FinalT], FinalT | Awaitable[FinalT | None] | None],
    ) -> ResponseStream[UpdateT, FinalT]:
        """Register a result hook executed after finalization."""
        self._result_hooks.append(hook)
        self._finalized = False
        self._final_result = None
        return self

    def with_cleanup_hook(
        self,
        hook: Callable[[], Awaitable[None] | None],
    ) -> ResponseStream[UpdateT, FinalT]:
        """Register a cleanup hook executed after stream consumption (before finalizer)."""
        self._cleanup_hooks.append(hook)
        return self

    def with_pull_context_manager(
        self,
        cm_factory: Callable[[], contextlib.AbstractContextManager[Any]],
    ) -> ResponseStream[UpdateT, FinalT]:
        """Register a context manager factory invoked around each underlying iterator pull.

        The factory is called once per ``__anext__`` and the returned context manager wraps
        the await of the underlying iterator. This is useful for state that needs to be
        active while the inner async work runs - for example, attaching an OpenTelemetry
        span to the current context so child spans created by inner code (HTTP clients,
        tool execution) are correctly parented.

        Because the context manager is entered and exited within the same ``__anext__``
        invocation, attach/detach style operations remain symmetric in the same async
        context regardless of where the stream is iterated.
        """
        self._pull_context_manager_factories.append(cm_factory)
        return self

    async def _run_cleanup_hooks(self) -> None:
        if self._cleanup_run:
            return
        self._cleanup_run = True
        hook_errors: list[BaseException] = []
        for hook in self._cleanup_hooks:
            try:
                result = hook()
                if isawaitable(result):
                    await result
            except BaseException as exc:
                hook_errors.append(exc)
        if hook_errors:
            _raise_stream_teardown_errors(hook_errors)

    @property
    def updates(self) -> Sequence[UpdateT]:
        return self._updates


# region ChatOptions


class ToolMode(TypedDict, total=False):
    """Tool choice mode for the chat options.

    Fields:
        mode: One of "auto", "required", or "none".
        required_function_name: Optional function name when `mode == "required"`.
        allowed_tools: Optional list of tool names when `mode` is `"auto"` or `"required"`.
    """

    mode: Literal["auto", "required", "none"]
    required_function_name: str
    allowed_tools: list[str]


# region TypedDict-based Chat Options


class _ChatOptionsBase(TypedDict, total=False):
    """Common request settings for AI services as a TypedDict.

    All fields are optional (total=False) to allow partial specification.
    Provider-specific TypedDicts extend this with additional options.

    These options represent the common denominator across chat providers.
    Individual implementations may raise errors for unsupported options.

    Examples:
        .. code-block:: python

            from chrys.kernel._types import ChatOptions, ToolMode

            # Type-safe options
            options: ChatOptions = {
                "temperature": 0.7,
                "max_tokens": 1000,
                "model": "gpt-4",
            }

            # With tools
            options_with_tools: ChatOptions = {
                "model": "gpt-4",
                "tool_choice": "auto",
                "temperature": 0.7,
            }

            # Used with Unpack for function signatures
            # async def get_response(self, **options: Unpack[ChatOptions]) -> ChatResponse:
    """

    # Model selection
    model: str
    # Generation parameters
    temperature: float
    top_p: float
    max_tokens: int
    stop: str | Sequence[str]
    seed: ReadOnly[int | None]
    logit_bias: ReadOnly[dict[str | int, float] | None]

    # Penalty parameters
    frequency_penalty: ReadOnly[float | None]
    presence_penalty: ReadOnly[float | None]

    # Tool configuration (forward reference to avoid circular import)
    tools: ToolTypes | Callable[..., Any] | Sequence[ToolTypes | Callable[..., Any]] | None
    tool_choice: ToolMode | Literal["auto", "required", "none"]
    allow_multiple_tool_calls: bool

    # Metadata
    metadata: dict[str, Any]
    user: str
    store: ReadOnly[bool | None]
    conversation_id: ReadOnly[str | None]

    # System/instructions
    instructions: str


if TYPE_CHECKING:

    class ChatOptions(_ChatOptionsBase, Generic[ResponseModelT], total=False):
        response_format: type[ResponseModelT] | Mapping[str, Any] | None

else:
    ChatOptions = _ChatOptionsBase


# region Chat Options Utility Functions


async def validate_chat_options(options: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize chat options dictionary.

    Validates numeric constraints and converts types as needed.

    Args:
        options: The options dictionary to validate.

    Returns:
        The validated and normalized options dictionary.

    Raises:
        ValueError: If any option value is invalid.

    Examples:
        .. code-block:: python

            from chrys.kernel._types import validate_chat_options

            options = await validate_chat_options({
                "temperature": 0.7,
                "max_tokens": 1000,
            })
    """
    result = dict(options)  # Make a copy

    # Validate numeric constraints
    if (freq_pen := result.get("frequency_penalty")) is not None:
        if not (-2.0 <= freq_pen <= 2.0):
            raise ValueError("frequency_penalty must be between -2.0 and 2.0")
        result["frequency_penalty"] = float(freq_pen)

    if (pres_pen := result.get("presence_penalty")) is not None:
        if not (-2.0 <= pres_pen <= 2.0):
            raise ValueError("presence_penalty must be between -2.0 and 2.0")
        result["presence_penalty"] = float(pres_pen)

    if (temp := result.get("temperature")) is not None:
        if not (0.0 <= temp <= 2.0):
            raise ValueError("temperature must be between 0.0 and 2.0")
        result["temperature"] = float(temp)

    if (top_p := result.get("top_p")) is not None:
        if not (0.0 <= top_p <= 1.0):
            raise ValueError("top_p must be between 0.0 and 1.0")
        result["top_p"] = float(top_p)

    if (max_tokens := result.get("max_tokens")) is not None and max_tokens <= 0:
        raise ValueError("max_tokens must be greater than 0")

    # Validate and normalize tools
    if "tools" in result:
        result["tools"] = await validate_tools(result["tools"])

    return result


def normalize_tools(
    tools: ToolTypes | Callable[..., Any] | Sequence[ToolTypes | Callable[..., Any]] | None,
) -> list[ToolTypes]:
    """Normalize tools into a list.

    Converts callables to FunctionTool objects and preserves existing tool objects.

    Args:
        tools: Tools to normalize - can be a single tool, callable, or sequence.

    Returns:
        Normalized list of tools.

    Examples:
        .. code-block:: python

            from chrys.kernel import normalize_tools, tool


            @tool
            def my_tool(x: int) -> int:
                return x * 2


            # Single tool
            tools = normalize_tools(my_tool)

            # List of tools
            tools = normalize_tools([my_tool, another_tool])
    """
    from .tools import normalize_tools as _normalize_tools

    return _normalize_tools(tools)


async def validate_tools(
    tools: ToolTypes | Callable[..., Any] | Sequence[ToolTypes | Callable[..., Any]] | None,
) -> list[ToolTypes]:
    """Validate and normalize tools into a list.

    Converts callables to FunctionTool objects, expands MCP tools to their constituent
    functions (connecting them if needed), while preserving non-callable tool objects.

    Args:
        tools: Tools to validate - can be a single tool, callable, or sequence.

    Returns:
        Normalized list of tools, or None if no tools provided.

    Examples:
        .. code-block:: python

            from chrys.kernel import tool
            from chrys.kernel._types import validate_tools


            @tool
            def my_tool(x: int) -> int:
                return x * 2


            # Single tool
            tools = await validate_tools(my_tool)

            # List of tools
            tools = await validate_tools([my_tool, another_tool])
    """
    # Use normalize_tools for common sync logic (converts callables to FunctionTool)
    normalized = normalize_tools(tools)

    expander = _get_tool_expander()
    final_tools: list[ToolTypes] = []
    for tool_ in normalized:
        expanded = await expander(tool_) if expander is not None else None
        if expanded is None:
            final_tools.append(tool_)
        else:
            final_tools.extend(expanded)

    return final_tools


def validate_tool_mode(
    tool_choice: ToolMode | Literal["auto", "required", "none"] | None,
) -> ToolMode | None:
    """Validate and normalize tool_choice to a ToolMode dict.

    Args:
        tool_choice: The tool choice value to validate.

    Returns:
        A ToolMode dict (contains keys: "mode", and optionally
        "required_function_name" or "allowed_tools"), or ``None`` when not provided.

    Raises:
        ContentError: If the tool_choice string is invalid.
    """
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        if tool_choice not in ("auto", "required", "none"):
            raise ContentError(f"Invalid tool choice: {tool_choice}")
        return {"mode": tool_choice}
    if "mode" not in tool_choice:
        raise ContentError("tool_choice dict must contain 'mode' key")
    if tool_choice["mode"] not in ("auto", "required", "none"):
        raise ContentError(f"Invalid tool choice: {tool_choice['mode']}")
    if tool_choice["mode"] != "required" and "required_function_name" in tool_choice:
        raise ContentError("tool_choice with mode other than 'required' cannot have 'required_function_name'")
    if tool_choice["mode"] not in ("auto", "required") and "allowed_tools" in tool_choice:
        raise ContentError("tool_choice 'allowed_tools' is only valid when mode is 'auto' or 'required'")
    if "allowed_tools" in tool_choice:
        allowed_tools = tool_choice["allowed_tools"]
        if isinstance(allowed_tools, str) or not isinstance(allowed_tools, Sequence):
            raise ContentError("tool_choice 'allowed_tools' must be a non-string sequence of strings")
        if not all(isinstance(tool_name, str) for tool_name in allowed_tools):
            raise ContentError("tool_choice 'allowed_tools' must contain only strings")
        normalized_tool_choice = dict(tool_choice)
        normalized_tool_choice["allowed_tools"] = list(allowed_tools)
        return cast(ToolMode, normalized_tool_choice)
    return tool_choice


def _tool_option_items(value: Any) -> list[Any]:
    """A ``tools`` option as a list of tools: a single tool, a mapping included, is one item.

    Only a collection is unpacked. Any other iterable, such as a generator, stays
    one item, so this merge never consumes a one-shot iterator the run still needs.
    """
    if isinstance(value, (str, bytes, bytearray, Mapping)) or not isinstance(value, Collection):
        return [value]
    return list(cast(Collection[Any], value))


def merge_chat_options(
    base: dict[str, Any] | None,
    override: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge two chat options dictionaries.

    Values from override take precedence over base.
    Lists and dicts are combined (not replaced).
    Instructions are concatenated with newlines.

    Args:
        base: The base options dictionary.
        override: The override options dictionary.

    Returns:
        A new merged options dictionary.

    Examples:
        .. code-block:: python

            from chrys.kernel._types import merge_chat_options

            base = {"temperature": 0.5, "model": "gpt-4"}
            override = {"temperature": 0.7, "max_tokens": 1000}
            merged = merge_chat_options(base, override)
            # {"temperature": 0.7, "model": "gpt-4", "max_tokens": 1000}
    """
    if not base:
        return dict(override) if override else {}
    if not override:
        return dict(base)

    result: dict[str, Any] = {}

    # Copy base values (shallow copy for simple values, dict copy for dicts)
    for key, value in base.items():
        if isinstance(value, dict):
            result[key] = dict(value)  # type: ignore[reportUnknownArgumentType]
        elif isinstance(value, list):
            result[key] = list(value)  # type: ignore[reportUnknownArgumentType]
        else:
            result[key] = value

    # Apply overrides
    for key, value in override.items():
        if value is None:
            continue

        if key == "instructions":
            # Concatenate instructions
            base_instructions = result.get("instructions")
            if base_instructions:
                result["instructions"] = f"{base_instructions}\n{value}"
            else:
                result["instructions"] = value
        elif key == "tools":
            # Merge tools lists
            base_tools = result.get("tools")
            if base_tools and value:
                # Add tools that aren't already present
                merged_tools = _tool_option_items(base_tools)
                for tool in _tool_option_items(value):
                    if tool not in merged_tools:
                        merged_tools.append(tool)
                result["tools"] = merged_tools
            elif value:
                result["tools"] = _tool_option_items(value)
        elif key in ("logit_bias", "metadata", "additional_properties"):
            # Merge dicts
            base_dict = result.get(key)
            if base_dict and isinstance(base_dict, dict) and isinstance(value, dict):
                result[key] = {**base_dict, **value}
            elif value:
                result[key] = dict(cast(Mapping[Any, Any], value)) if isinstance(value, dict) else value
        elif key == "tool_choice":
            # tool_choice from override takes precedence
            result["tool_choice"] = value or result.get("tool_choice")
        elif key == "response_format":
            # response_format from override takes precedence if set
            result["response_format"] = value
        else:
            # Simple override
            result[key] = value

    return result
