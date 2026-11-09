# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Microsoft Agent Framework (MIT License; see NOTICE).

"""Chrys-owned content types."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from copy import deepcopy
from typing import Any, ClassVar, Final, Literal, TypeGuard, TypeVar, cast

from typing_extensions import TypedDict

from chrys.foundation.hosted_tools import HostedToolFamily
from chrys.foundation.reasoning_origin import REASONING_ORIGIN_KEY
from chrys.foundation.text.model_json import model_json

from .exceptions import AdditionItemMismatch, ContentError

logger = logging.getLogger(__name__)

OPENAI_OUTPUT_MESSAGE_ENVELOPE_KEY: Final[str] = "openai.responses.output_message_envelope"
# What tells the output items of a Responses stream apart on the function
# calls it sends, each whole: its position and its item id. Two items are two
# calls even under one call id, so their calls never merge.
_RESPONSES_OUTPUT_ITEM_KEYS: Final = ("output_index", "fc_id")
_ANTHROPIC_REDACTED_THINKING_KEY = "anthropic_redacted_thinking"


# region Content Parsing Utilities
def _parse_content_list(contents_data: Sequence[Any]) -> list[Content]:
    """Parse a list of content data into appropriate Content objects.

    Args:
        contents_data: List of content data (strings, dicts, or already constructed objects).
            A single string is one text item.

    Returns:
        List of Content objects with unknown types logged and ignored
    """
    if isinstance(contents_data, str):
        # A string is itself a sequence of strings: iterating it would make
        # one text content per character.
        contents_data = [contents_data]
    contents: list[Content] = []
    for content_data in contents_data:
        if content_data is None:
            continue
        if isinstance(content_data, Content):
            contents.append(content_data)
            continue
        if isinstance(content_data, str):
            contents.append(Content.from_text(text=content_data))
            continue
        try:
            contents.append(Content.from_dict(content_data))
        except ContentError as exc:
            logger.warning(f"Skipping unknown content type or invalid content: {exc}")

    return contents


# region Internal Helper functions for unified Content


def detect_media_type_from_base64(
    *,
    data_bytes: bytes | None = None,
    data_str: str | None = None,
    data_uri: str | None = None,
) -> str | None:
    """Detect media type from base64-encoded data by examining magic bytes.

    This function examines the binary signature (magic bytes) at the start of the data
    to identify common media types. It's reliable for binary formats like images, audio,
    video, and documents, but cannot detect text-based formats like JSON or plain text.

    Args:
        data_bytes: Raw binary data.
        data_str: Base64-encoded data (without data URI prefix).
        data_uri: Full data URI string (e.g., "data:image/png;base64,iVBORw0KGgo...").
            This will look at the actual data to determine the media_type and not at the URI prefix.
            Will also not compare those two values.

    Returns:
        The detected media type (e.g., 'image/png', 'audio/wav', 'application/pdf')
        or None if the format is not recognized.

    Raises:
        ValueError: If not exactly 1 of data_bytes, data_str, or data_uri is provided, or if base64 decoding fails.

    Examples:
        .. code-block:: python

            from chrys.kernel._content import detect_media_type_from_base64

            # Detect from base64 string
            base64_data = "iVBORw0KGgo..."
            media_type = detect_media_type_from_base64(base64_data)
            # Returns: "image/png"

            # Works with data URIs too
            data_uri = "data:image/png;base64,iVBORw0KGgo..."
            media_type = detect_media_type_from_base64(data_uri)
            # Returns: "image/png"
    """
    data: bytes | None = None
    if data_bytes is not None:
        data = data_bytes
    if data_uri is not None:
        if data is not None:
            raise ValueError("Provide exactly one of data_bytes, data_str, or data_uri.")
        # Remove data URI prefix if present
        if not data_uri.startswith("data:") or "," not in data_uri:
            raise ValueError("Invalid data URI format.")
        prefix, data_str = data_uri.split(",", 1)
        if not prefix.endswith(";base64"):
            raise ValueError("Data URI must use base64 encoding.")
    if data_str is not None:
        if data is not None:
            raise ValueError("Provide exactly one of data_bytes, data_str, or data_uri.")
        try:
            data = base64.b64decode(data_str)
        except Exception as exc:
            raise ValueError("Invalid base64 data provided.") from exc
    if data is None:
        raise ValueError("Provide exactly one of data_bytes, data_str, or data_uri.")

    # Check magic bytes for common formats
    # Images
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and len(data) > 11 and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    if data.startswith((b"<svg", b"<?xml")):
        return "image/svg+xml"

    # Documents
    if data.startswith(b"%PDF-"):
        return "application/pdf"

    # Audio
    if data.startswith(b"RIFF") and len(data) > 11 and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith((b"ID3", b"\xff\xfb", b"\xff\xf3")):
        return "audio/mpeg"
    if data.startswith(b"OggS"):
        return "audio/ogg"
    if data.startswith(b"fLaC"):
        return "audio/flac"

    return None


def _get_data_bytes_as_str(content: Content) -> str | None:
    """Extract base64 data string from data URI.

    Args:
        content: The Content instance to extract data from.

    Returns:
        The base64-encoded data as a string, or None if not a data content type.

    Raises:
        ContentError: If the URI is not a valid data URI.
    """
    if content.type not in ("data", "uri"):
        return None

    uri = content.uri
    if not uri:
        return None

    if not uri.startswith("data:"):
        return None

    if ";base64," not in uri:
        raise ContentError("Data URI must use base64 encoding")

    _, data = uri.split(";base64,", 1)
    return data  # type: ignore[return-value, no-any-return]


def _get_data_bytes(content: Content) -> bytes | None:  # pyright: ignore[reportUnusedFunction]
    """Extract and decode binary data from data URI.

    Args:
        content: The Content instance to extract data from.

    Returns:
        The decoded binary data, or None if not a data content type.

    Raises:
        ContentError: If the URI is not a valid data URI or decoding fails.
    """
    data_str = _get_data_bytes_as_str(content)
    if data_str is None:
        return None

    try:
        return base64.b64decode(data_str)
    except Exception as e:
        raise ContentError(f"Failed to decode base64 data: {e}") from e


KNOWN_URI_SCHEMAS: Final[set[str]] = {"http", "https", "ftp", "ftps", "file", "s3", "gs", "azure", "blob"}


def _validate_uri(uri: str, media_type: str | None) -> dict[str, Any]:
    """Validate URI format and return validation result.

    Args:
        uri: The URI to validate.
        media_type: Optional media type associated with the URI.

    Returns:
        If valid, returns a dict, with "type" key indicating "data" or "uri", along with the uri and media_type.
    """
    if not uri:
        raise ContentError("URI cannot be empty")

    # Check for data URI
    if uri.startswith("data:"):
        if "," not in uri:
            raise ContentError("Data URI must contain a comma separating metadata and data")
        prefix, _ = uri.split(",", 1)
        if ";" in prefix:
            parts = prefix.split(";")
            if len(parts) < 2:
                raise ContentError("Invalid data URI format")
            # Check encoding
            encoding = parts[-1]
            if encoding not in ("base64", ""):
                raise ContentError(f"Unsupported data URI encoding: {encoding}")
            if media_type is None:
                # attempt to extract:
                media_type = parts[0][5:]  # Remove 'data:'
        return {"type": "data", "uri": uri, "media_type": media_type}

    # Check for common URI schemes
    if ":" in uri:
        scheme = uri.split(":", 1)[0].lower()
        if not media_type:
            logger.warning("Using URI without media type is not recommended.")
        if scheme not in KNOWN_URI_SCHEMAS:
            logger.info(f"Unknown URI scheme: {scheme}, allowed schemes are {KNOWN_URI_SCHEMAS}.")
        return {"type": "uri", "uri": uri, "media_type": media_type}

    # No scheme found
    raise ContentError("URI must contain a scheme (e.g., http://, data:, file://)")


def _serialize_value(value: Any, exclude_none: bool) -> Any:
    """Recursively serialize a value for to_dict."""
    if value is None:
        return None
    if isinstance(value, Content):
        return value.to_dict(exclude_none=exclude_none)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_serialize_value(item, exclude_none) for item in cast(Iterable[Any], value)]
    if isinstance(value, Mapping):
        return {k: _serialize_value(v, exclude_none) for k, v in value.items()}  # type: ignore[reportUnknownVariableType]
    if hasattr(value, "to_dict"):
        return value.to_dict()  # type: ignore[call-arg]
    return value


def _restore_compaction_annotation_in_additional_properties(
    additional_properties: MutableMapping[str, Any] | None,
    *,
    allow_none: bool = False,
) -> dict[str, Any] | None:
    if additional_properties is None:
        return None if allow_none else {}

    return dict(additional_properties)


# region Constants and types
KNOWN_MEDIA_TYPES = [
    "application/json",
    "application/octet-stream",
    "application/pdf",
    "application/xml",
    "audio/mpeg",
    "audio/mp3",
    "audio/ogg",
    "audio/wav",
    "image/apng",
    "image/avif",
    "image/bmp",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/svg+xml",
    "image/tiff",
    "image/webp",
    "text/css",
    "text/csv",
    "text/html",
    "text/javascript",
    "text/plain",
    "text/plain;charset=UTF-8",
    "text/xml",
]

# region Unified Content Types

ContentType = Literal[
    "text",
    "text_reasoning",
    "data",
    "uri",
    "error",
    "function_call",
    "function_result",
    "usage",
    "hosted_file",
    "hosted_vector_store",
    "code_interpreter_tool_call",
    "code_interpreter_tool_result",
    "image_generation_tool_call",
    "image_generation_tool_result",
    "mcp_server_tool_call",
    "mcp_server_tool_result",
    "search_tool_call",
    "search_tool_result",
    "shell_tool_call",
    "shell_tool_result",
    "shell_command_output",
    "hosted_tool_call",
    "hosted_tool_result",
]


class TextSpanRegion(TypedDict, total=False):
    """TypedDict representation of a text span region annotation."""

    type: Literal["text_span"]
    start_index: int
    end_index: int


class Annotation(TypedDict, total=False):
    """TypedDict representation of an annotation."""

    type: Literal["citation"]
    title: str
    url: str
    file_id: str
    tool_name: str
    snippet: str | None
    annotated_regions: Sequence[TextSpanRegion]
    additional_properties: dict[str, Any]
    raw_representation: Any


ContentT = TypeVar("ContentT", bound="Content")

# endregion


class UsageDetails(TypedDict, total=False, extra_items=int):  # type: ignore[call-arg]
    """A dictionary representing usage details.

    This is a non-closed dictionary, so any specific provider fields can be added as needed.
    Whenever they can be mapped to standard fields, they will be.

    Keys:
        input_token_count: The number of input tokens used.
        output_token_count: The number of output tokens generated.
        total_token_count: The total number of tokens (input + output).
        context_input_token_count: The final request's context-window input occupancy.
        context_input_token_estimate: A provider-derived estimate of final input occupancy.
        context_input_token_floor: A provider-derived lower bound for final input occupancy.
        cache_creation_input_token_count: The number of input tokens written to a provider-managed cache.
        cache_read_input_token_count: The number of input tokens served from a provider-managed cache.
        reasoning_output_token_count: The number of output tokens used for reasoning.

    """

    input_token_count: int | None
    output_token_count: int | None
    total_token_count: int | None
    context_input_token_count: int | None
    context_input_token_estimate: int | None
    context_input_token_floor: int | None
    cache_creation_input_token_count: int | None
    cache_read_input_token_count: int | None
    reasoning_output_token_count: int | None


def _is_content_list(value: list[Any]) -> TypeGuard[list[Content]]:
    """Return whether every item is a Chrys ``Content`` instance."""
    return all(isinstance(item, Content) for item in value)


def add_usage_details(usage1: UsageDetails | None, usage2: UsageDetails | None) -> UsageDetails:
    """Add two UsageDetails dictionaries by summing all numeric values.

    If any of the two usage details contains a key with a non-int value, it will be skipped,
    even if the other contains a int-value on that key.

    Args:
        usage1: First usage details dictionary.
        usage2: Second usage details dictionary.

    Returns:
        A new UsageDetails dictionary with summed values.

    Examples:
        .. code-block:: python

            from chrys.kernel._content import UsageDetails, add_usage_details

            usage1 = UsageDetails(input_token_count=5, output_token_count=10)
            usage2 = UsageDetails(input_token_count=3, output_token_count=6)
            combined = add_usage_details(usage1, usage2)
            # Result: {'input_token_count': 8, 'output_token_count': 16}
    """
    if usage1 is None:
        return usage2 or UsageDetails()
    if usage2 is None:
        return usage1

    result = UsageDetails()
    # Combine all keys from both dictionaries
    all_keys = set(usage1.keys()) | set(usage2.keys())
    for key in all_keys:
        if not isinstance((val1 := usage1.get(key, 0)), (int | None)) or not isinstance(
            (val2 := usage2.get(key, 0)), (int | None)
        ):
            logger.warning("Non `int` value found in usage details, skipping.")
            continue
        result[key] = (val1 or 0) + (val2 or 0)  # type: ignore[literal-required]
    return result


def normalize_stream_usage(usages: Sequence[Mapping[str, Any]]) -> UsageDetails | None:
    """Collapse cumulative usage snapshots from one streamed model call.

    Streaming providers may emit more than one cumulative usage payload for a
    single request.  Summing those snapshots over-counts the request; the
    latest non-null value for each key is the authoritative per-call value.

    Aggregation *across distinct model calls* remains the responsibility of
    the caller via :func:`add_usage_details`.
    """
    if not usages:
        return None

    merged = UsageDetails()
    for usage in usages:
        for key, value in usage.items():
            if value is not None:
                merged[key] = value  # type: ignore[literal-required,typeddict-item]
    return merged


# region Content Class
class Content:
    """Unified content container covering all content variants.

    This class provides a single unified type that handles all content variants.
    Use the class methods like `Content.from_text()`, `Content.from_data()`,
    `Content.from_uri()`, etc. to create instances.
    """

    _SHALLOW_COPY_FIELDS: ClassVar[set[str]] = {"raw_representation"}
    __hash__ = None

    def __init__(
        self,
        type: ContentType,
        *,
        # Text content fields
        text: str | None = None,
        protected_data: str | None = None,
        # Data/URI content fields
        uri: str | None = None,
        media_type: str | None = None,
        # Error content fields
        message: str | None = None,
        error_code: str | None = None,
        error_details: str | None = None,
        # Usage content fields
        usage_details: UsageDetails | None = None,
        # Function call/result fields
        call_id: str | None = None,
        name: str | None = None,
        arguments: str | Mapping[str, Any] | None = None,
        informational_only: bool = False,
        exception: str | None = None,
        result: Any = None,
        items: Sequence[Content] | None = None,
        # Hosted file/vector store fields
        file_id: str | None = None,
        vector_store_id: str | None = None,
        # Code interpreter tool fields
        inputs: list[Content] | None = None,
        outputs: list[Content] | Any | None = None,
        # Image generation tool fields
        image_id: str | None = None,
        # Shell tool fields
        commands: list[str] | None = None,
        timeout_ms: int | None = None,
        max_output_length: int | None = None,
        status: str | None = None,
        # Shell command output fields
        stdout: str | None = None,
        stderr: str | None = None,
        exit_code: int | None = None,
        timed_out: bool | None = None,
        # MCP server tool fields
        tool_name: str | None = None,
        server_name: str | None = None,
        output: Any = None,
        # Server-issued item identity (e.g. Responses reasoning ``rs_*`` ids)
        id: str | None = None,
        # Provider-hosted tool fields
        provider_hosted: bool = False,
        hosted_family: str | None = None,
        hosted_provider: str | None = None,
        provider_item_type: str | None = None,
        provider_item_id: str | None = None,
        provider_phase: str | None = None,
        provider_status: str | None = None,
        retry_safety: str | None = None,
        # Common fields
        annotations: Sequence[Annotation] | None = None,
        additional_properties: MutableMapping[str, Any] | None = None,
        raw_representation: Any | None = None,
    ) -> None:
        """Create a content instance.

        Prefer using the classmethod constructors like `Content.from_text()` instead of calling __init__ directly.
        """
        self.type = type
        self.annotations = annotations
        self.additional_properties: dict[str, Any] = (
            _restore_compaction_annotation_in_additional_properties(additional_properties) or {}
        )
        self.raw_representation = raw_representation

        # Set all content-specific attributes
        self.text = text
        self.protected_data = protected_data
        self.uri = uri
        self.media_type = media_type
        self.message = message
        self.error_code = error_code
        self.error_details = error_details
        self.usage_details = usage_details
        self.call_id = call_id
        self.name = name
        self.arguments = arguments
        self.informational_only = informational_only or type == "mcp_server_tool_call"
        self.exception = exception
        self.result = result
        self.items = items
        self.file_id = file_id
        self.vector_store_id = vector_store_id
        self.inputs = inputs
        self.outputs = outputs
        self.image_id = image_id
        self.commands = commands
        self.timeout_ms = timeout_ms
        self.max_output_length = max_output_length
        self.status = status
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.tool_name = tool_name
        self.server_name = server_name
        self.output = output
        self.id = id
        self.provider_hosted = provider_hosted
        self.hosted_family = hosted_family
        self.hosted_provider = hosted_provider
        self.provider_item_type = provider_item_type
        self.provider_item_id = provider_item_id
        self.provider_phase = provider_phase
        self.provider_status = provider_status
        self.retry_safety = retry_safety

    def __deepcopy__(self, memo: dict[int, Any]) -> Content:
        """Create a deep copy, preserving ``_SHALLOW_COPY_FIELDS`` by reference.

        Fields listed in ``_SHALLOW_COPY_FIELDS`` may contain LLM SDK objects
        (e.g., proto/gRPC responses) that are not safe to deep-copy.
        """
        cls = type(self)
        result = cls.__new__(cls)
        memo[id(self)] = result
        shallow = cls._SHALLOW_COPY_FIELDS
        for k, v in self.__dict__.items():
            if k in shallow:
                object.__setattr__(result, k, v)
            else:
                object.__setattr__(result, k, deepcopy(v, memo))
        return result

    @classmethod
    def from_text(
        cls: type[ContentT],
        text: str,
        *,
        annotations: Sequence[Annotation] | None = None,
        additional_properties: MutableMapping[str, Any] | None = None,
        raw_representation: Any = None,
    ) -> ContentT:
        """Create text content."""
        return cls(
            "text",
            text=text,
            annotations=annotations,
            additional_properties=additional_properties,
            raw_representation=raw_representation,
        )

    @classmethod
    def from_text_reasoning(
        cls: type[ContentT],
        *,
        id: str | None = None,
        text: str | None = None,
        protected_data: str | None = None,
        annotations: Sequence[Annotation] | None = None,
        additional_properties: MutableMapping[str, Any] | None = None,
        raw_representation: Any = None,
    ) -> ContentT:
        """Create text reasoning content."""
        return cls(
            "text_reasoning",
            id=id,
            text=text,
            protected_data=protected_data,
            annotations=annotations,
            additional_properties=additional_properties,
            raw_representation=raw_representation,
        )

    @classmethod
    def from_data(
        cls: type[ContentT],
        data: bytes,
        media_type: str,
        *,
        annotations: Sequence[Annotation] | None = None,
        additional_properties: MutableMapping[str, Any] | None = None,
        raw_representation: Any = None,
    ) -> ContentT:
        r"""Create data content from raw binary data.

        Use this to create content from binary data (images, audio, documents, etc.).
        The data will be automatically base64-encoded into a data URI.

        Args:
            data: Raw binary data as bytes. This should be the actual binary data,
                not a base64-encoded string. If you have a base64 string,
                decode it first: base64.b64decode(base64_string)
            media_type: The MIME type of the data (e.g., "image/png", "application/pdf").
                If you don't know the media type and have base64 data, you can detect it in some cases:

                .. code-block:: python

                    from chrys.kernel._content import detect_media_type_from_base64, Content

                    media_type = detect_media_type_from_base64(base64_string)
                    if media_type is None:
                        raise ValueError("Could not detect media type")
                    data_bytes = base64.b64decode(base64_string)
                    content = Content.from_data(data=data_bytes, media_type=media_type)

        Keyword Args:
            annotations: Optional annotations associated with the content.
            additional_properties: Optional additional properties.
            raw_representation: Optional raw representation from an underlying implementation.

        Returns:
            A Content instance with type="data".

        Raises:
            TypeError: If data is not bytes.

        Examples:
            .. code-block:: python

                from chrys.kernel._content import Content, detect_media_type_from_base64
                import base64

                # Create from raw binary data with known media type
                image_bytes = b"\x89PNG\r\n\x1a\n..."
                content = Content.from_data(data=image_bytes, media_type="image/png")

                # If you have a base64 string and need to detect media type
                base64_string = "iVBORw0KGgo..."
                media_type = detect_media_type_from_base64(base64_string)
                if media_type is None:
                    raise ValueError("Unknown media type")
                image_bytes = base64.b64decode(base64_string)
                content = Content.from_data(data=image_bytes, media_type=media_type)
        """
        try:
            encoded_data = base64.b64encode(data).decode("utf-8")
        except TypeError as e:
            raise TypeError(
                "Could not encode data to base64. Ensure 'data' is of type bytes.Or another b64encode compatible type."
            ) from e
        return cls(
            "data",
            uri=f"data:{media_type};base64,{encoded_data}",
            media_type=media_type,
            annotations=annotations,
            additional_properties=additional_properties,
            raw_representation=raw_representation,
        )

    @classmethod
    def from_uri(
        cls: type[ContentT],
        uri: str,
        *,
        media_type: str | None = None,
        annotations: Sequence[Annotation] | None = None,
        additional_properties: MutableMapping[str, Any] | None = None,
        raw_representation: Any = None,
    ) -> ContentT:
        """Create content from a URI, can be both data URI or external URI.

        Use this when you already have a properly formed data URI
        (e.g., "data:image/png;base64,iVBORw0KGgo...").
        Or when you receive a link to a online resource (e.g., "https://example.com/image.png").

        Args:
            uri: A URI string,
                that either includes the media type and base64-encoded data,
                or a valid URL to an external resource.

        Keyword Args:
            media_type: The MIME type of the data (e.g., "image/png", "application/pdf").
                This is optional but recommended for external URIs.
            annotations: Optional annotations associated with the content.
            additional_properties: Optional additional properties.
            raw_representation: Optional raw representation from an underlying implementation.

        Returns:
            A Content instance with type="data" for data URIs or type="uri" for external URIs.

        Raises:
            ContentError: If the URI is not valid.

        Examples:
            .. code-block:: python

                from chrys.kernel._content import Content

                # Create from a data URI
                content = Content.from_uri(uri="data:image/png;base64,iVBORw0KGgo...", media_type="image/png")
                assert content.type == "data"

                # Create from an external URI
                content = Content.from_uri(uri="https://example.com/image.png", media_type="image/png")
                assert content.type == "uri"

                # When receiving a raw already encode data string, you can do this:
                raw_base64_string = "iVBORw0KGgo..."
                content = Content.from_uri(
                    uri=f"data:{(detect_media_type_from_base64(data_str=raw_base64_string) or 'image/png')};base64,{
                        raw_base64_string
                    }"
                )
        """
        return cls(
            **_validate_uri(uri, media_type),
            annotations=annotations,
            additional_properties=additional_properties,
            raw_representation=raw_representation,
        )

    @classmethod
    def from_error(
        cls: type[ContentT],
        *,
        message: str | None = None,
        error_code: str | None = None,
        error_details: str | None = None,
        annotations: Sequence[Annotation] | None = None,
        additional_properties: MutableMapping[str, Any] | None = None,
        raw_representation: Any = None,
    ) -> ContentT:
        """Create error content."""
        return cls(
            "error",
            message=message,
            error_code=error_code,
            error_details=error_details,
            annotations=annotations,
            additional_properties=additional_properties,
            raw_representation=raw_representation,
        )

    @classmethod
    def from_function_call(
        cls: type[ContentT],
        call_id: str,
        name: str,
        *,
        arguments: str | Mapping[str, Any] | None = None,
        informational_only: bool = False,
        exception: str | None = None,
        annotations: Sequence[Annotation] | None = None,
        additional_properties: MutableMapping[str, Any] | None = None,
        raw_representation: Any = None,
    ) -> ContentT:
        """Create function call content.

        ``informational_only=True`` preserves a provider-hosted call in the
        transcript without authorizing the local tool loop to execute it. The
        flag is serialized only when true so older sessions retain their wire
        shape; sessions containing a true flag require a build that supports
        this field and are not downgrade-compatible with older Chrys builds.
        """
        return cls(
            "function_call",
            call_id=call_id,
            name=name,
            arguments=arguments,
            informational_only=informational_only,
            exception=exception,
            annotations=annotations,
            additional_properties=additional_properties,
            raw_representation=raw_representation,
        )

    @classmethod
    def from_function_result(
        cls: type[ContentT],
        call_id: str | None,
        *,
        result: Any = None,
        exception: str | None = None,
        annotations: Sequence[Annotation] | None = None,
        additional_properties: MutableMapping[str, Any] | None = None,
        raw_representation: Any = None,
    ) -> ContentT:
        """Create function result content.

        All tool output is represented uniformly as Content items in the
        ``items`` field.  The ``result`` field is populated with the concatenated
        text from text items for backwards compatibility.

        Args:
            call_id: The ID of the function call this result corresponds to.

        Keyword Args:
            result: The tool output.  Accepts a ``list[Content]`` (the canonical
                form produced by :meth:`~FunctionTool.parse_result`), a plain
                ``str``, or any other value (which is stringified).
            exception: The exception message if the function call failed.
            annotations: Optional annotations for the content.
            additional_properties: Optional additional properties.
            raw_representation: Optional raw representation from the provider.
        """
        if isinstance(result, list):
            if _is_content_list(result):
                items_list: list[Content] = list(result)
            else:
                items_list = [Content.from_text(str(result))]  # type: ignore[reportUnknownArgumentType]
        elif isinstance(result, str):
            items_list = [Content.from_text(result)]
        elif result is not None:
            try:
                text = model_json(result, default=str)
            except TypeError, ValueError:
                text = str(result)
            items_list = [Content.from_text(text)]
        else:
            items_list = [Content.from_text("")]

        text_parts = [c.text for c in items_list if c.type == "text" and c.text]
        text_result = "\n".join(text_parts) if text_parts else ""

        return cls(
            "function_result",
            call_id=call_id,
            result=text_result,
            items=items_list,
            exception=exception,
            annotations=annotations,
            additional_properties=additional_properties,
            raw_representation=raw_representation,
        )

    @classmethod
    def from_search_tool_call(
        cls: type[ContentT],
        call_id: str,
        *,
        tool_name: str,
        arguments: str | Mapping[str, Any] | None = None,
        status: str | None = None,
        hosted_family: str = HostedToolFamily.SEARCH,
        hosted_provider: str | None = None,
        provider_item_type: str | None = None,
        provider_item_id: str | None = None,
        provider_phase: str | None = None,
        provider_status: str | None = None,
        retry_safety: str | None = None,
        annotations: Sequence[Annotation] | None = None,
        additional_properties: MutableMapping[str, Any] | None = None,
        raw_representation: Any = None,
    ) -> ContentT:
        """Create search tool call content."""
        return cls(
            "search_tool_call",
            call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
            status=status,
            provider_hosted=True,
            hosted_family=hosted_family,
            hosted_provider=hosted_provider,
            provider_item_type=provider_item_type,
            provider_item_id=provider_item_id,
            provider_phase=provider_phase,
            provider_status=provider_status if provider_status is not None else status,
            retry_safety=retry_safety,
            annotations=annotations,
            additional_properties=additional_properties,
            raw_representation=raw_representation,
        )

    @classmethod
    def from_search_tool_result(
        cls: type[ContentT],
        call_id: str,
        *,
        tool_name: str,
        result: Any = None,
        items: Sequence[Content] | None = None,
        status: str | None = None,
        hosted_family: str = HostedToolFamily.SEARCH,
        hosted_provider: str | None = None,
        provider_item_type: str | None = None,
        provider_item_id: str | None = None,
        provider_phase: str | None = None,
        provider_status: str | None = None,
        retry_safety: str | None = None,
        annotations: Sequence[Annotation] | None = None,
        additional_properties: MutableMapping[str, Any] | None = None,
        raw_representation: Any = None,
    ) -> ContentT:
        """Create search tool result content."""
        return cls(
            "search_tool_result",
            call_id=call_id,
            tool_name=tool_name,
            result=result,
            items=list(items) if items is not None else None,
            status=status,
            provider_hosted=True,
            hosted_family=hosted_family,
            hosted_provider=hosted_provider,
            provider_item_type=provider_item_type,
            provider_item_id=provider_item_id,
            provider_phase=provider_phase,
            provider_status=provider_status if provider_status is not None else status,
            retry_safety=retry_safety,
            annotations=annotations,
            additional_properties=additional_properties,
            raw_representation=raw_representation,
        )

    @classmethod
    def from_hosted_tool_call(
        cls: type[ContentT],
        call_id: str | None,
        *,
        tool_name: str,
        arguments: Any = None,
        status: str | None = None,
        hosted_family: str = HostedToolFamily.GENERIC,
        hosted_provider: str | None = None,
        provider_item_type: str | None = None,
        provider_item_id: str | None = None,
        provider_phase: str | None = None,
        provider_status: str | None = None,
        retry_safety: str | None = None,
        annotations: Sequence[Annotation] | None = None,
        additional_properties: MutableMapping[str, Any] | None = None,
        raw_representation: Any = None,
    ) -> ContentT:
        """Create generic provider-hosted tool call content."""
        return cls(
            "hosted_tool_call",
            call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
            status=status,
            provider_hosted=True,
            hosted_family=hosted_family,
            hosted_provider=hosted_provider,
            provider_item_type=provider_item_type,
            provider_item_id=provider_item_id,
            provider_phase=provider_phase,
            provider_status=provider_status if provider_status is not None else status,
            retry_safety=retry_safety,
            annotations=annotations,
            additional_properties=additional_properties,
            raw_representation=raw_representation,
        )

    @classmethod
    def from_hosted_tool_result(
        cls: type[ContentT],
        call_id: str | None,
        *,
        tool_name: str | None = None,
        result: Any = None,
        items: Sequence[Content] | None = None,
        status: str | None = None,
        hosted_family: str = HostedToolFamily.GENERIC,
        hosted_provider: str | None = None,
        provider_item_type: str | None = None,
        provider_item_id: str | None = None,
        provider_phase: str | None = None,
        provider_status: str | None = None,
        retry_safety: str | None = None,
        annotations: Sequence[Annotation] | None = None,
        additional_properties: MutableMapping[str, Any] | None = None,
        raw_representation: Any = None,
    ) -> ContentT:
        """Create generic provider-hosted tool result content."""
        return cls(
            "hosted_tool_result",
            call_id=call_id,
            tool_name=tool_name,
            result=result,
            items=list(items) if items is not None else None,
            status=status,
            provider_hosted=True,
            hosted_family=hosted_family,
            hosted_provider=hosted_provider,
            provider_item_type=provider_item_type,
            provider_item_id=provider_item_id,
            provider_phase=provider_phase,
            provider_status=provider_status if provider_status is not None else status,
            retry_safety=retry_safety,
            annotations=annotations,
            additional_properties=additional_properties,
            raw_representation=raw_representation,
        )

    @classmethod
    def from_usage(
        cls: type[ContentT],
        usage_details: UsageDetails,
        *,
        annotations: Sequence[Annotation] | None = None,
        additional_properties: MutableMapping[str, Any] | None = None,
        raw_representation: Any = None,
    ) -> ContentT:
        """Create usage content."""
        return cls(
            "usage",
            usage_details=usage_details,
            annotations=annotations,
            additional_properties=additional_properties,
            raw_representation=raw_representation,
        )

    @classmethod
    def from_hosted_file(
        cls: type[ContentT],
        file_id: str,
        *,
        media_type: str | None = None,
        name: str | None = None,
        annotations: Sequence[Annotation] | None = None,
        additional_properties: MutableMapping[str, Any] | None = None,
        raw_representation: Any = None,
    ) -> ContentT:
        """Create hosted file content."""
        return cls(
            "hosted_file",
            file_id=file_id,
            media_type=media_type,
            name=name,
            annotations=annotations,
            additional_properties=additional_properties,
            raw_representation=raw_representation,
        )

    @classmethod
    def from_hosted_vector_store(
        cls: type[ContentT],
        vector_store_id: str,
        *,
        annotations: Sequence[Annotation] | None = None,
        additional_properties: MutableMapping[str, Any] | None = None,
        raw_representation: Any = None,
    ) -> ContentT:
        """Create hosted vector store content."""
        return cls(
            "hosted_vector_store",
            vector_store_id=vector_store_id,
            annotations=annotations,
            additional_properties=additional_properties,
            raw_representation=raw_representation,
        )

    @classmethod
    def from_code_interpreter_tool_call(
        cls: type[ContentT],
        *,
        call_id: str | None = None,
        inputs: Sequence[Content] | None = None,
        hosted_provider: str | None = None,
        provider_item_type: str | None = None,
        provider_item_id: str | None = None,
        provider_phase: str | None = None,
        provider_status: str | None = None,
        retry_safety: str | None = None,
        annotations: Sequence[Annotation] | None = None,
        additional_properties: MutableMapping[str, Any] | None = None,
        raw_representation: Any = None,
    ) -> ContentT:
        """Create code interpreter tool call content."""
        return cls(
            "code_interpreter_tool_call",
            call_id=call_id,
            inputs=list(inputs) if inputs is not None else None,
            provider_hosted=True,
            hosted_family=HostedToolFamily.CODE,
            hosted_provider=hosted_provider,
            provider_item_type=provider_item_type,
            provider_item_id=provider_item_id,
            provider_phase=provider_phase,
            provider_status=provider_status,
            retry_safety=retry_safety,
            annotations=annotations,
            additional_properties=additional_properties,
            raw_representation=raw_representation,
        )

    @classmethod
    def from_code_interpreter_tool_result(
        cls: type[ContentT],
        *,
        call_id: str | None = None,
        outputs: Sequence[Content] | None = None,
        hosted_provider: str | None = None,
        provider_item_type: str | None = None,
        provider_item_id: str | None = None,
        provider_phase: str | None = None,
        provider_status: str | None = None,
        retry_safety: str | None = None,
        annotations: Sequence[Annotation] | None = None,
        additional_properties: MutableMapping[str, Any] | None = None,
        raw_representation: Any = None,
    ) -> ContentT:
        """Create code interpreter tool result content."""
        return cls(
            "code_interpreter_tool_result",
            call_id=call_id,
            outputs=list(outputs) if outputs is not None else None,
            provider_hosted=True,
            hosted_family=HostedToolFamily.CODE,
            hosted_provider=hosted_provider,
            provider_item_type=provider_item_type,
            provider_item_id=provider_item_id,
            provider_phase=provider_phase,
            provider_status=provider_status,
            retry_safety=retry_safety,
            annotations=annotations,
            additional_properties=additional_properties,
            raw_representation=raw_representation,
        )

    @classmethod
    def from_image_generation_tool_call(
        cls: type[ContentT],
        *,
        image_id: str | None = None,
        hosted_provider: str | None = None,
        provider_item_type: str | None = None,
        provider_item_id: str | None = None,
        provider_phase: str | None = None,
        provider_status: str | None = None,
        retry_safety: str | None = None,
        annotations: Sequence[Annotation] | None = None,
        additional_properties: MutableMapping[str, Any] | None = None,
        raw_representation: Any = None,
    ) -> ContentT:
        """Create image generation tool call content."""
        return cls(
            "image_generation_tool_call",
            image_id=image_id,
            provider_hosted=True,
            hosted_family=HostedToolFamily.IMAGE,
            hosted_provider=hosted_provider,
            provider_item_type=provider_item_type,
            provider_item_id=provider_item_id,
            provider_phase=provider_phase,
            provider_status=provider_status,
            retry_safety=retry_safety,
            annotations=annotations,
            additional_properties=additional_properties,
            raw_representation=raw_representation,
        )

    @classmethod
    def from_image_generation_tool_result(
        cls: type[ContentT],
        *,
        image_id: str | None = None,
        outputs: Any = None,
        hosted_provider: str | None = None,
        provider_item_type: str | None = None,
        provider_item_id: str | None = None,
        provider_phase: str | None = None,
        provider_status: str | None = None,
        retry_safety: str | None = None,
        annotations: Sequence[Annotation] | None = None,
        additional_properties: MutableMapping[str, Any] | None = None,
        raw_representation: Any = None,
    ) -> ContentT:
        """Create image generation tool result content."""
        return cls(
            "image_generation_tool_result",
            image_id=image_id,
            outputs=outputs,
            provider_hosted=True,
            hosted_family=HostedToolFamily.IMAGE,
            hosted_provider=hosted_provider,
            provider_item_type=provider_item_type,
            provider_item_id=provider_item_id,
            provider_phase=provider_phase,
            provider_status=provider_status,
            retry_safety=retry_safety,
            annotations=annotations,
            additional_properties=additional_properties,
            raw_representation=raw_representation,
        )

    @classmethod
    def from_shell_tool_call(
        cls: type[ContentT],
        *,
        call_id: str | None = None,
        commands: list[str] | None = None,
        timeout_ms: int | None = None,
        max_output_length: int | None = None,
        status: str | None = None,
        hosted_provider: str | None = None,
        provider_item_type: str | None = None,
        provider_item_id: str | None = None,
        provider_phase: str | None = None,
        provider_status: str | None = None,
        retry_safety: str | None = None,
        annotations: Sequence[Annotation] | None = None,
        additional_properties: MutableMapping[str, Any] | None = None,
        raw_representation: Any = None,
    ) -> ContentT:
        """Create shell tool call content.

        This content represents the model's request to run one or more shell
        commands. It is request metadata, not command output.

        Keyword Args:
            call_id: The unique identifier for this tool call.
            commands: The list of commands to execute.
            timeout_ms: The timeout in milliseconds for the shell command execution.
            max_output_length: The maximum output length in characters.
            status: The status of the shell call (e.g., "in_progress", "completed", "incomplete").
            annotations: Optional annotations for this content.
            additional_properties: Optional additional properties.
            raw_representation: The raw provider-specific representation.
        """
        return cls(
            "shell_tool_call",
            call_id=call_id,
            commands=commands,
            timeout_ms=timeout_ms,
            max_output_length=max_output_length,
            status=status,
            provider_hosted=True,
            hosted_family=HostedToolFamily.SHELL,
            hosted_provider=hosted_provider,
            provider_item_type=provider_item_type,
            provider_item_id=provider_item_id,
            provider_phase=provider_phase,
            provider_status=provider_status if provider_status is not None else status,
            retry_safety=retry_safety,
            annotations=annotations,
            additional_properties=additional_properties,
            raw_representation=raw_representation,
        )

    @classmethod
    def from_shell_tool_result(
        cls: type[ContentT],
        *,
        call_id: str | None = None,
        outputs: Sequence[Content] | None = None,
        max_output_length: int | None = None,
        hosted_provider: str | None = None,
        provider_item_type: str | None = None,
        provider_item_id: str | None = None,
        provider_phase: str | None = None,
        provider_status: str | None = None,
        retry_safety: str | None = None,
        annotations: Sequence[Annotation] | None = None,
        additional_properties: MutableMapping[str, Any] | None = None,
        raw_representation: Any = None,
    ) -> ContentT:
        """Create shell tool result content.

        This content represents the aggregate result for a shell tool call.
        Use :meth:`from_shell_command_output` to build each per-command output
        item and pass those objects via ``outputs``.

        Keyword Args:
            call_id: The function call ID for which this is the result.
            outputs: The list of shell command output Content objects.
            max_output_length: The maximum output length in characters.
            annotations: Optional annotations for this content.
            additional_properties: Optional additional properties.
            raw_representation: The raw provider-specific representation.
        """
        return cls(
            "shell_tool_result",
            call_id=call_id,
            outputs=list(outputs) if outputs is not None else None,
            max_output_length=max_output_length,
            provider_hosted=True,
            hosted_family=HostedToolFamily.SHELL,
            hosted_provider=hosted_provider,
            provider_item_type=provider_item_type,
            provider_item_id=provider_item_id,
            provider_phase=provider_phase,
            provider_status=provider_status,
            retry_safety=retry_safety,
            annotations=annotations,
            additional_properties=additional_properties,
            raw_representation=raw_representation,
        )

    @classmethod
    def from_shell_command_output(
        cls: type[ContentT],
        *,
        stdout: str | None = None,
        stderr: str | None = None,
        exit_code: int | None = None,
        timed_out: bool | None = None,
        additional_properties: MutableMapping[str, Any] | None = None,
        raw_representation: Any = None,
    ) -> ContentT:
        """Create shell command output content for one command execution.

        Keyword Args:
            stdout: The standard output of the command.
            stderr: The standard error output of the command.
            exit_code: The exit code of the command, or None if the command timed out.
            timed_out: Whether the command execution timed out.
            additional_properties: Optional additional properties.
            raw_representation: The raw provider-specific representation.
        """
        return cls(
            "shell_command_output",
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            timed_out=timed_out,
            additional_properties=additional_properties,
            raw_representation=raw_representation,
        )

    @classmethod
    def from_mcp_server_tool_call(
        cls: type[ContentT],
        call_id: str,
        tool_name: str,
        *,
        server_name: str | None = None,
        arguments: str | Mapping[str, Any] | None = None,
        hosted_provider: str | None = None,
        provider_item_type: str | None = None,
        provider_item_id: str | None = None,
        provider_phase: str | None = None,
        provider_status: str | None = None,
        retry_safety: str | None = None,
        annotations: Sequence[Annotation] | None = None,
        additional_properties: MutableMapping[str, Any] | None = None,
        raw_representation: Any = None,
    ) -> ContentT:
        """Create MCP server tool call content."""
        return cls(
            "mcp_server_tool_call",
            call_id=call_id,
            tool_name=tool_name,
            server_name=server_name,
            arguments=arguments,
            informational_only=True,
            provider_hosted=True,
            hosted_family=HostedToolFamily.MCP,
            hosted_provider=hosted_provider,
            provider_item_type=provider_item_type,
            provider_item_id=provider_item_id,
            provider_phase=provider_phase,
            provider_status=provider_status,
            retry_safety=retry_safety,
            annotations=annotations,
            additional_properties=additional_properties,
            raw_representation=raw_representation,
        )

    @classmethod
    def from_mcp_server_tool_result(
        cls: type[ContentT],
        call_id: str,
        *,
        output: Any = None,
        hosted_provider: str | None = None,
        provider_item_type: str | None = None,
        provider_item_id: str | None = None,
        provider_phase: str | None = None,
        provider_status: str | None = None,
        retry_safety: str | None = None,
        annotations: Sequence[Annotation] | None = None,
        additional_properties: MutableMapping[str, Any] | None = None,
        raw_representation: Any = None,
    ) -> ContentT:
        """Create MCP server tool result content."""
        return cls(
            "mcp_server_tool_result",
            call_id=call_id,
            output=output,
            provider_hosted=True,
            hosted_family=HostedToolFamily.MCP,
            hosted_provider=hosted_provider,
            provider_item_type=provider_item_type,
            provider_item_id=provider_item_id,
            provider_phase=provider_phase,
            provider_status=provider_status,
            retry_safety=retry_safety,
            annotations=annotations,
            additional_properties=additional_properties,
            raw_representation=raw_representation,
        )

    def to_dict(self, *, exclude_none: bool = True, exclude: set[str] | None = None) -> dict[str, Any]:
        """Serialize the content to a dictionary."""
        fields_to_capture = (
            "text",
            "protected_data",
            "uri",
            "media_type",
            "message",
            "error_code",
            "error_details",
            "usage_details",
            "call_id",
            "name",
            "arguments",
            "informational_only",
            "exception",
            "result",
            "items",
            "file_id",
            "vector_store_id",
            "inputs",
            "outputs",
            "image_id",
            "commands",
            "timeout_ms",
            "max_output_length",
            "status",
            "stdout",
            "stderr",
            "exit_code",
            "timed_out",
            "tool_name",
            "server_name",
            "output",
            "id",
            "provider_hosted",
            "hosted_family",
            "hosted_provider",
            "provider_item_type",
            "provider_item_id",
            "provider_phase",
            "provider_status",
            "retry_safety",
            "additional_properties",
        )

        exclude = exclude or set()
        result: dict[str, Any] = {"type": self.type}

        for field in fields_to_capture:
            value = getattr(self, field, None)
            if field in exclude:
                continue
            # Keep old/ordinary session payloads byte-shape compatible. Only
            # informational function calls need this explicit marker; hosted
            # MCP calls are inherently informational by content type.
            if field == "informational_only" and (self.type != "function_call" or not value):
                continue
            if field == "provider_hosted" and not value:
                continue
            if (
                field
                in {
                    "hosted_family",
                    "hosted_provider",
                    "provider_item_type",
                    "provider_item_id",
                    "provider_phase",
                    "provider_status",
                    "retry_safety",
                }
                and value is None
            ):
                continue
            if exclude_none and value is None:
                continue
            result[field] = _serialize_value(value, exclude_none)

        if "annotations" not in exclude and self.annotations is not None:
            result["annotations"] = [
                {
                    key: _serialize_value(value, exclude_none)
                    for key, value in annotation.items()
                    if key != "raw_representation"
                }
                for annotation in self.annotations
            ]

        return result

    def __eq__(self, other: object) -> bool:
        """Check if two Content instances are equal by comparing their dict representations."""
        if not isinstance(other, Content):
            return False
        return self.to_dict(exclude_none=False) == other.to_dict(exclude_none=False)

    def __str__(self) -> str:
        """Return a string representation of the Content."""
        if self.type == "error":
            if self.error_code:
                return f"Error {self.error_code}: {self.message or ''}"
            return self.message or "Unknown error"
        if self.type == "text":
            return self.text or ""
        return f"Content(type={self.type})"

    @classmethod
    def from_dict(cls: type[ContentT], data: Mapping[str, Any]) -> ContentT:
        """Create a Content instance from a mapping."""
        if not (content_type := data.get("type")):
            raise ValueError("Content mapping requires 'type'")
        remaining = dict(data)
        remaining.pop("type", None)
        annotations = remaining.pop("annotations", None)
        additional_properties = remaining.pop("additional_properties", None)
        raw_representation = remaining.pop("raw_representation", None)

        # Special handling for DataContent with data and media_type
        if content_type == "data" and "data" in remaining and "media_type" in remaining:
            # Use from_data() to properly create the DataContent with URI
            return cls.from_data(remaining["data"], remaining["media_type"])

        # Handle list of Content objects (e.g., inputs in code_interpreter_tool_call)
        if (input_items := remaining.get("inputs")) and isinstance(input_items, list):
            remaining["inputs"] = [cls.from_dict(item) if isinstance(item, dict) else item for item in input_items]  # type: ignore[reportUnknownVariableType]
        if (output_items := remaining.get("outputs")) and isinstance(output_items, list):
            remaining["outputs"] = [cls.from_dict(item) if isinstance(item, dict) else item for item in output_items]  # type: ignore[reportUnknownVariableType]
        if (content_items := remaining.get("items")) and isinstance(content_items, list):
            remaining["items"] = [cls.from_dict(item) if isinstance(item, dict) else item for item in content_items]  # type: ignore[reportUnknownVariableType]

        return cls(
            type=content_type,
            annotations=annotations,
            additional_properties=additional_properties,
            raw_representation=raw_representation,
            **remaining,
        )

    def __add__(self, other: Content) -> Content:
        """Concatenate or merge two Content instances."""
        if not isinstance(other, Content):
            raise TypeError(f"Incompatible type: Cannot add Content with {type(other).__name__}")

        if self.type != other.type:
            raise TypeError(f"Cannot add Content of type '{self.type}' with type '{other.type}'")

        if self.type == "text":
            return self._add_text_content(other)
        if self.type == "text_reasoning":
            return self._add_text_reasoning_content(other)
        if self.type == "function_call":
            return self._add_function_call_content(other)
        if self.type == "usage":
            return self._add_usage_content(other)
        raise ContentError(f"Addition not supported for content type: {self.type}")

    def _add_text_content(self, other: Content) -> Content:
        """Add two TextContent instances.

        ``_ContentRun`` folds a streamed run without calling this: keep its
        ``accepts`` and ``fold`` in step with the rules here.
        """
        if self.text is None or other.text is None:
            raise ContentError("Cannot add text content when either text value is None")
        if self.additional_properties.get(OPENAI_OUTPUT_MESSAGE_ENVELOPE_KEY) != other.additional_properties.get(
            OPENAI_OUTPUT_MESSAGE_ENVELOPE_KEY
        ):
            raise AdditionItemMismatch("Cannot merge text contents from different OpenAI Responses output messages")
        return Content(
            "text",
            text=self.text + other.text,
            annotations=_combine_annotations(self.annotations, other.annotations),
            additional_properties=_combine_additional_props(self.additional_properties, other.additional_properties),
            raw_representation=_combine_raw_representations(self.raw_representation, other.raw_representation),
        )

    def _add_text_reasoning_content(self, other: Content) -> Content:
        """Add two TextReasoningContent instances.

        ``_ContentRun`` folds a streamed run without calling this: keep its
        ``accepts`` and ``fold`` in step with the rules here.
        """
        # Ensure we do not silently merge contents with conflicting ids
        if self.id and other.id and self.id != other.id:
            raise AdditionItemMismatch(
                f"Cannot add text_reasoning content with different ids: {self.id!r} != {other.id!r}"
            )
        combined_id = self.id or other.id

        # Reasoning captured under different wire dialects must never merge:
        # each side's replay format is provider state, not concatenable text.
        if self.additional_properties.get("openai_reasoning_format") != other.additional_properties.get(
            "openai_reasoning_format"
        ):
            raise AdditionItemMismatch("Cannot merge reasoning contents with different wire formats")
        if self.additional_properties.get(_ANTHROPIC_REDACTED_THINKING_KEY) != other.additional_properties.get(
            _ANTHROPIC_REDACTED_THINKING_KEY
        ):
            raise AdditionItemMismatch("Cannot merge redacted and ordinary Anthropic reasoning contents")
        # Each side replays only to the endpoint that issued it.
        if self.additional_properties.get(REASONING_ORIGIN_KEY) != other.additional_properties.get(
            REASONING_ORIGIN_KEY
        ):
            raise AdditionItemMismatch("Cannot merge reasoning contents from different endpoints")

        # Concatenate text, handling None values
        self_text = self.text or ""  # type: ignore[attr-defined]
        other_text = other.text or ""  # type: ignore[attr-defined]
        if (
            self_text
            and other_text
            and ("reasoning_text" in self.additional_properties) != ("reasoning_text" in other.additional_properties)
        ):
            raise AdditionItemMismatch("Cannot merge reasoning text with a reasoning summary")
        combined_text = self_text + other_text if (self_text or other_text) else None

        # Handle protected_data replacement. Two id-less sides that BOTH carry
        # distinct opaque payloads cannot merge by replacement — that silently
        # drops the earlier payload. Equal non-empty ids keep later-wins: the
        # ids already matched above, and a same-id pair is the Responses
        # snapshot→final refinement contract, where the later payload is the
        # terminal one.
        if (
            self.protected_data is not None
            and other.protected_data is not None
            and self.protected_data != other.protected_data
            and not (self.id and other.id)
        ):
            raise AdditionItemMismatch("Cannot merge reasoning contents that both carry distinct protected payloads")
        protected_data = other.protected_data if other.protected_data is not None else self.protected_data  # type: ignore[attr-defined]

        return Content(
            "text_reasoning",
            id=combined_id,
            text=combined_text,
            protected_data=protected_data,
            annotations=_combine_annotations(self.annotations, other.annotations),
            additional_properties=_combine_additional_props(self.additional_properties, other.additional_properties),
            raw_representation=_combine_raw_representations(self.raw_representation, other.raw_representation),
        )

    def _add_function_call_content(self, other: Content) -> Content:
        """Add two FunctionCallContent instances."""
        other_call_id = other.call_id
        self_call_id = self.call_id
        if other_call_id and self_call_id != other_call_id:
            raise ContentError("Cannot add function calls with different call_ids")
        for key in _RESPONSES_OUTPUT_ITEM_KEYS:
            mine, theirs = self.additional_properties.get(key), other.additional_properties.get(key)
            if mine is not None and theirs is not None and mine != theirs:
                raise AdditionItemMismatch("Cannot merge function calls from different OpenAI Responses output items")

        self_arguments = self.arguments
        other_arguments = other.arguments

        if not self_arguments:
            arguments: str | Mapping[str, Any] | None = other_arguments
        elif not other_arguments:
            arguments = self_arguments
        elif isinstance(self_arguments, str) and isinstance(other_arguments, str):
            arguments = self_arguments + other_arguments
        elif isinstance(self_arguments, dict) and isinstance(other_arguments, dict):
            arguments = {**self_arguments, **other_arguments}
        else:
            raise TypeError("Incompatible argument types")

        return Content(
            "function_call",
            call_id=self_call_id,
            name=self.name or other.name,
            arguments=arguments,
            informational_only=self.informational_only or other.informational_only,
            exception=self.exception or other.exception,
            additional_properties=_combine_additional_props(self.additional_properties, other.additional_properties),
            raw_representation=_combine_raw_representations(self.raw_representation, other.raw_representation),
        )

    def _add_usage_content(self, other: Content) -> Content:
        """Add two UsageContent instances by combining their usage details."""
        return Content(
            "usage",
            usage_details=add_usage_details(self.usage_details, other.usage_details),
            additional_properties=_combine_additional_props(self.additional_properties, other.additional_properties),
            raw_representation=_combine_raw_representations(self.raw_representation, other.raw_representation),
        )

    def has_top_level_media_type(self, top_level_media_type: Literal["application", "audio", "image", "text"]) -> bool:
        """Check if content has a specific top-level media type.

        Works with data, uri, and hosted_file content types.

        Args:
            top_level_media_type: The top-level media type to check for.

        Returns:
            True if the content's media type matches the specified top-level type.

        Raises:
            ContentError: If the content type doesn't support media types.

        Examples:
            .. code-block:: python

                from chrys.kernel._content import Content

                image = Content.from_uri(uri="data:image/png;base64,abc123", media_type="image/png")
                print(image.has_top_level_media_type("image"))  # True
                print(image.has_top_level_media_type("audio"))  # False
        """
        if self.media_type is None:
            raise ContentError("no media_type found")

        slash_index = self.media_type.find("/")
        span = self.media_type[:slash_index] if slash_index >= 0 else self.media_type
        span = span.strip()
        return span.lower() == top_level_media_type.lower()

    def parse_arguments(self) -> Mapping[str, Any] | None:
        """Parse arguments from function_call, mcp_server_tool_call, or search_tool_call content.

        If arguments cannot be parsed as JSON or the result is not a dict,
        they are returned as a dictionary with a single key "raw".

        Returns:
            Parsed arguments as a dictionary, or None if no arguments.

        Raises:
            ContentError: If the content type doesn't support arguments.

        Examples:
            .. code-block:: python

                from chrys.kernel._content import Content

                func_call = Content.from_function_call(
                    call_id="call_123",
                    name="send_email",
                    arguments='{"to": "user@example.com"}',
                )
                args = func_call.parse_arguments()
                print(args)  # {"to": "user@example.com"}
        """
        if self.arguments is None:
            return None

        if not self.arguments:
            return {}

        if isinstance(self.arguments, str):
            # If arguments are a string, try to parse it as JSON
            try:
                loaded = json.loads(self.arguments)
                if isinstance(loaded, dict):
                    return loaded
                return {"raw": loaded}
            except json.JSONDecodeError, TypeError:
                return {"raw": self.arguments}
        return self.arguments


def _combine_additional_props(
    self_additional_properties: dict[str, Any], other_additional_properties: dict[str, Any]
) -> dict[str, Any]:
    """Combine additional properties for addition operations."""
    return {
        **other_additional_properties,
        **self_additional_properties,
    }


def _combine_raw_representations(
    self_repr: Any,
    other_repr: Any,
) -> Any:
    """Combine raw representations for addition operations."""
    if self_repr is None:
        return other_repr
    if other_repr is None:
        return self_repr
    self_list = self_repr if isinstance(self_repr, list) else [self_repr]  # type: ignore[reportUnknownVariableType]
    other_list = other_repr if isinstance(other_repr, list) else [other_repr]  # type: ignore[reportUnknownVariableType]
    return self_list + other_list  # type: ignore[reportUnknownVariableType]


def _combine_annotations(
    self_annotations: Sequence[Annotation] | None,
    other_annotations: Sequence[Annotation] | None,
) -> Sequence[Annotation] | None:
    """Combine annotations for addition operations."""
    if self_annotations is None:
        return other_annotations
    if other_annotations is None:
        return self_annotations
    return [*self_annotations, *other_annotations]


# Keys whose value, as the earliest chunk carrying the key holds it, decides
# whether the next chunk may join a folded run (see ``_add_text_content`` and
# ``_add_text_reasoning_content``).
_TEXT_RUN_SPLIT_KEYS: Final = (OPENAI_OUTPUT_MESSAGE_ENVELOPE_KEY,)
_REASONING_RUN_SPLIT_KEYS: Final = (
    "openai_reasoning_format",
    _ANTHROPIC_REDACTED_THINKING_KEY,
    REASONING_ORIGIN_KEY,
)


class _ContentRun:
    """A run of streamed ``text`` or ``text_reasoning`` chunks, folded in one pass.

    Folding a stream with repeated ``+`` rebuilds the text, the property dict,
    the annotation list and the raw-representation list at every step, so
    ``n`` chunks cost O(n²). A run instead keeps only what each ``+`` checks
    (the earliest value of each split key, the id, whether any text or
    reasoning-text marker was seen, the latest protected payload) and builds
    the folded content once. The result is the repeated-``+`` fold's, from a
    deep copy of the head: ``accepts`` mirrors where ``+`` raises and ``fold``
    what it builds, so change them together with ``_add_text_content`` and
    ``_add_text_reasoning_content`` (a fuzz test pins the two folds equal).
    """

    __slots__ = (
        "_chunks",
        "_has_reasoning_text_key",
        "_has_text",
        "_id",
        "_protected_data",
        "_split_keys",
        "_split_values",
    )

    def __init__(self, head: Content) -> None:
        if head.type not in ("text", "text_reasoning"):
            raise ContentError(f"Cannot fold a run of {head.type} content")
        self._split_keys = _TEXT_RUN_SPLIT_KEYS if head.type == "text" else _REASONING_RUN_SPLIT_KEYS
        self._chunks = [head]
        self._split_values: dict[str, Any] = {}
        self._id = head.id
        self._has_text = bool(head.text)
        self._has_reasoning_text_key = False
        self._protected_data = head.protected_data
        self._note(head)

    def accepts(self, other: Content) -> bool:
        """Whether ``+`` would add ``other`` to the folded run (False where it raises a mismatch)."""
        head = self._chunks[0]
        other_props = other.additional_properties
        if head.type == "text":
            if head.text is None or other.text is None:
                raise ContentError("Cannot add text content when either text value is None")
            return self._split_values.get(OPENAI_OUTPUT_MESSAGE_ENVELOPE_KEY) == other_props.get(
                OPENAI_OUTPUT_MESSAGE_ENVELOPE_KEY
            )
        if self._id and other.id and self._id != other.id:
            return False
        if any(self._split_values.get(key) != other_props.get(key) for key in self._split_keys):
            return False
        if self._has_text and other.text and self._has_reasoning_text_key != ("reasoning_text" in other_props):
            return False
        return not (
            self._protected_data is not None
            and other.protected_data is not None
            and self._protected_data != other.protected_data
            and not (self._id and other.id)
        )

    def add(self, other: Content) -> None:
        """Append a chunk ``accepts`` let in."""
        self._chunks.append(other)
        self._id = self._id or other.id
        self._has_text = self._has_text or bool(other.text)
        if other.protected_data is not None:
            self._protected_data = other.protected_data
        self._note(other)

    def _note(self, chunk: Content) -> None:
        props = chunk.additional_properties
        for key in self._split_keys:
            if key in props and key not in self._split_values:
                self._split_values[key] = props[key]
        if "reasoning_text" in props:
            self._has_reasoning_text_key = True

    def fold(self) -> Content:
        """The run as one content: what folding it with repeated ``+`` from a copy of its head gives."""
        head = deepcopy(self._chunks[0])
        if len(self._chunks) == 1:
            return head
        rest = self._chunks[1:]
        # ``+`` keeps the left side's value for a key both sides carry, so the
        # earliest chunk wins; updating latest-first also keeps ``+``'s key order.
        additional_properties: dict[str, Any] = {}
        for chunk in reversed(rest):
            additional_properties.update(chunk.additional_properties)
        additional_properties.update(head.additional_properties)
        annotation_runs = [
            annotations
            for annotations in (head.annotations, *(chunk.annotations for chunk in rest))
            if annotations is not None
        ]
        annotations: Sequence[Annotation] | None
        if len(annotation_runs) <= 1:
            annotations = annotation_runs[0] if annotation_runs else None
        else:
            annotations = [annotation for run in annotation_runs for annotation in run]
        raws = [
            raw for raw in (head.raw_representation, *(chunk.raw_representation for chunk in rest)) if raw is not None
        ]
        raw_representation: Any
        if len(raws) <= 1:
            raw_representation = raws[0] if raws else None
        else:
            raw_representation = []
            for raw in raws:
                if isinstance(raw, list):
                    raw_representation.extend(cast(list[Any], raw))
                else:
                    raw_representation.append(raw)
        if head.type == "text":
            return Content(
                "text",
                text="".join(cast(str, chunk.text) for chunk in (head, *rest)),
                annotations=annotations,
                additional_properties=additional_properties,
                raw_representation=raw_representation,
            )
        return Content(
            "text_reasoning",
            id=self._id,
            text="".join(chunk.text or "" for chunk in (head, *rest)) if self._has_text else None,
            protected_data=self._protected_data,
            annotations=annotations,
            additional_properties=additional_properties,
            raw_representation=raw_representation,
        )


def _text_for_join(content: Content) -> Any:
    """Leave validation of the nullable field to ``str.join``, preserving its ``TypeError``."""
    return content.text
