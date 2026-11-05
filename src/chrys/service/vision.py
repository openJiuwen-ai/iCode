# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-agent image capability wiring."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from chrys.kernel import GROUP_ANNOTATION_KEY, Content, FunctionTool, Message
from chrys.kernel import is_image_content as _is_image_content
from chrys.kernel.compaction import GROUP_TOKEN_COUNT_KEY, GROUP_TOKEN_ESTIMATOR_VERSION_KEY
from chrys.kernel.middleware import ChatContext, ChatMiddleware

VIEW_IMAGE_TOOL_NAME = "view_image"
# Persisted with history: token counts may describe the temporary text view
# even though the stored contents still contain the original image.
_IMAGE_STUB_TOKEN_VIEW_KEY = "_chrys_image_stub_token_view"


def filter_image_tools(tools: Sequence[Any], *, vision_enabled: bool) -> list[Any]:
    """Return tools with image-reading tools removed for text-only agents."""
    if vision_enabled:
        return list(tools)
    return [item for item in tools if not (isinstance(item, FunctionTool) and item.name == VIEW_IMAGE_TOOL_NAME)]


def image_stub_middleware_for_model(*, vision_enabled: bool) -> ChatMiddleware:
    """Prepare image views and invalidate counts when their representation changes."""
    if vision_enabled:
        return VisionImageTokenCacheMiddleware()
    return NonVisionImageStubMiddleware()


def invalidate_image_token_count(message: Message) -> None:
    """Drop only the estimate, preserving shared exclusion and summary metadata."""
    annotation = message.additional_properties.get(GROUP_ANNOTATION_KEY)
    if isinstance(annotation, dict):
        annotation = dict(annotation)
        annotation.pop(GROUP_TOKEN_COUNT_KEY, None)
        annotation.pop(GROUP_TOKEN_ESTIMATOR_VERSION_KEY, None)
        message.additional_properties[GROUP_ANNOTATION_KEY] = annotation


class VisionImageTokenCacheMiddleware(ChatMiddleware):
    """Discard persisted placeholder estimates before sending full images."""

    async def process(self, context: ChatContext, call_next: Any) -> None:
        for message in context.messages:
            if message.additional_properties.pop(_IMAGE_STUB_TOKEN_VIEW_KEY, False):
                invalidate_image_token_count(message)
        await call_next()


class NonVisionImageStubMiddleware(ChatMiddleware):
    """Replace model-visible image content with text stubs for text-only models."""

    async def process(self, context: ChatContext, call_next: Any) -> None:
        replacement_messages: list[Message] = []
        changed = False
        for message in context.messages:
            replacement_contents: list[Content] = []
            message_changed = False
            for content in message.contents:
                replacement = _stub_content_images(content)
                replacement_contents.append(replacement)
                if replacement is not content:
                    message_changed = True
            if message_changed:
                # The history retains the image, but this call counts its stub.
                # Invalidate on every replacement: an earlier full-history
                # annotation may have counted the original image in between calls.
                invalidate_image_token_count(message)
                message.additional_properties[_IMAGE_STUB_TOKEN_VIEW_KEY] = True
                refreshed = Message(
                    message.role,
                    replacement_contents,
                    author_name=message.author_name,
                    message_id=message.message_id,
                    raw_representation=message.raw_representation,
                )
                # Downstream compaction flags must write through to owned history.
                refreshed.additional_properties = message.additional_properties
                replacement_messages.append(refreshed)
                changed = True
            else:
                replacement_messages.append(message)
        if changed:
            context.messages = replacement_messages
        await call_next()


def _stub_content_images(content: Content) -> Content:
    if content.type == "function_result" and content.items:
        replacement_items: list[Content] = []
        changed = False
        for item in content.items:
            replacement = _stub_direct_image(item)
            replacement_items.append(replacement)
            if replacement is not item:
                changed = True
        if not changed:
            return content
        return Content.from_function_result(
            content.call_id or "",
            result=replacement_items,
            exception=content.exception,
            annotations=content.annotations,
            additional_properties=content.additional_properties,
            raw_representation=content.raw_representation,
        )
    return _stub_direct_image(content)


def _stub_direct_image(content: Content) -> Content:
    if not _is_image_content(content):
        return content
    return Content.from_text(_image_stub_text(content), annotations=content.annotations)


def _image_stub_text(content: Content) -> str:
    props = content.additional_properties
    width = props.get("width")
    height = props.get("height")
    media_type = content.media_type or props.get("media_type") or "image"
    if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        return f"[image {width}x{height} {media_type} omitted: model is not vision-capable]"
    return f"[image {media_type} omitted: model is not vision-capable]"
