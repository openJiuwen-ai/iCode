# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ACP content conversion for Chrys user turns."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass

from acp.schema import (
    AudioContentBlock,
    BlobResourceContents,
    EmbeddedResourceContentBlock,
    ImageContentBlock,
    ResourceContentBlock,
    TextContentBlock,
    TextResourceContents,
)

from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.events.types import UserMessage
from chrys.foundation.text.images import (
    COMPRESSED_IMAGE_MEDIA_TYPE,
    MAX_ACP_IMAGE_BYTES,
    MAX_IMAGE_BYTES,
    ImageProcessingError,
    compress_image_data,
    format_mb,
    wire_image_media_type,
)
from chrys.kernel import Content

_MAX_BASE64_IMAGE_CHARS = ((MAX_ACP_IMAGE_BYTES + 2) // 3) * 4


class AcpContentError(ValueError):
    """Raised when ACP prompt content cannot be represented by Chrys."""


@dataclass(frozen=True)
class ConvertedPrompt:
    """Chrys user message derived from ACP prompt content."""

    message: UserMessage
    has_images: bool = False


ContentBlock = (
    TextContentBlock | ImageContentBlock | AudioContentBlock | ResourceContentBlock | EmbeddedResourceContentBlock
)


def convert_prompt_blocks(
    blocks: list[ContentBlock], *, vision_enabled: bool, max_image_bytes: int = MAX_IMAGE_BYTES
) -> ConvertedPrompt:
    """Convert ACP prompt blocks into a Chrys ``UserMessage``.

    An image over *max_image_bytes* is compressed here, which can take a
    while: a caller on an event loop runs this in a worker thread.
    """
    text_parts: list[str] = []
    image_contents: list[Content] = []

    for block in blocks:
        if isinstance(block, TextContentBlock):
            text_parts.append(block.text)
            continue
        if isinstance(block, ResourceContentBlock):
            text_parts.append(_format_resource_link(block))
            continue
        if isinstance(block, EmbeddedResourceContentBlock):
            text_parts.append(_format_embedded_resource(block))
            continue
        if isinstance(block, ImageContentBlock):
            if not vision_enabled:
                msg = "This session's model profile does not support image input."
                raise AcpContentError(msg)
            image_contents.append(_image_content(block, max_bytes=max_image_bytes))
            continue
        if isinstance(block, AudioContentBlock):
            msg = f"Audio content is not supported by {APP_DISPLAY_NAME} ACP phase 1."
            raise AcpContentError(msg)
        msg = f"Unsupported ACP content block: {type(block).__name__}"
        raise AcpContentError(msg)

    text = "\n\n".join(part for part in text_parts if part)
    message = UserMessage(text=text)
    if image_contents:
        message.prepared_contents = [text, *image_contents]
    return ConvertedPrompt(message=message, has_images=bool(image_contents))


def _format_resource_link(block: ResourceContentBlock) -> str:
    lines = [f"Resource link: {block.name}", f"URI: {block.uri}"]
    if block.title:
        lines.append(f"Title: {block.title}")
    if block.mime_type:
        lines.append(f"MIME type: {block.mime_type}")
    if block.description:
        lines.append(f"Description: {block.description}")
    if block.size is not None:
        lines.append(f"Size: {block.size} bytes")
    return "\n".join(lines)


def _format_embedded_resource(block: EmbeddedResourceContentBlock) -> str:
    resource = block.resource
    if isinstance(resource, TextResourceContents):
        heading = f"Embedded resource: {resource.uri}"
        if resource.mime_type:
            heading = f"{heading} ({resource.mime_type})"
        return f"{heading}\n\n{resource.text}"
    if isinstance(resource, BlobResourceContents):
        msg = f"Embedded blob resources are not supported by {APP_DISPLAY_NAME} ACP phase 1."
        raise AcpContentError(msg)
    msg = f"Unsupported embedded resource: {type(resource).__name__}"
    raise AcpContentError(msg)


def _image_content(block: ImageContentBlock, *, max_bytes: int) -> Content:
    mime_type = block.mime_type.strip().lower()
    if not mime_type.startswith("image/"):
        msg = f"Unsupported image MIME type: {block.mime_type}"
        raise AcpContentError(msg)
    if len(block.data) > _MAX_BASE64_IMAGE_CHARS:
        msg = f"Image content exceeds the {format_mb(MAX_ACP_IMAGE_BYTES)} limit."
        raise AcpContentError(msg)
    try:
        data = base64.b64decode(block.data, validate=True)
    except (binascii.Error, ValueError) as exc:
        msg = "Image content is not valid base64."
        raise AcpContentError(msg) from exc
    if not data:
        msg = "Image content is empty."
        raise AcpContentError(msg)
    if len(data) > MAX_ACP_IMAGE_BYTES:
        msg = f"Image content exceeds the {format_mb(MAX_ACP_IMAGE_BYTES)} limit."
        raise AcpContentError(msg)
    # The bytes name the type: a mislabelled or unreadable image would fail
    # every later request of the session.
    if (media_type := wire_image_media_type(data, None)) is None:
        msg = "Unsupported image format. Use PNG, JPEG, GIF, or WebP."
        raise AcpContentError(msg)
    if len(data) > max_bytes:
        try:
            data = compress_image_data(data, max_bytes=max_bytes)
        except ImageProcessingError as exc:
            raise AcpContentError(str(exc)) from exc
        media_type = COMPRESSED_IMAGE_MEDIA_TYPE
    return Content.from_data(data=data, media_type=media_type)
