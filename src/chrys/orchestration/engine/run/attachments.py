# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""User-message attachment helpers for multimodal input.

Pipeline:
- ``discover_image_references`` only parses image-looking ``@file`` tokens and
  resolves paths; it does not touch the filesystem. Use this before text-only
  model rejection so filesystem state cannot leak into the error choice.
- ``discover_image_mentions`` stats those references and validates existence,
  regular-file status, and source size without reading image bytes.
- ``load_image_attachments`` reads bytes for already validated mentions and
  compresses oversized images in memory before attaching them.
- ``parse_image_mentions`` is the convenience wrapper for tests and callers
  that intentionally want validation plus byte loading in one step.

Every scanner here reads only the user-authored body: a trailing
conversation-selections block (quoted transcript text some frontends append)
is spliced out first, so a quoted ``@file.png`` is never treated as an
attachment the user asked to send.
"""

from __future__ import annotations

import math
import stat as stat_module
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.i18n import DisplayBlock, MessageRef, msg
from chrys.foundation.text.conversation_selections import selections_span
from chrys.foundation.text.images import (
    COMPRESSED_IMAGE_MEDIA_TYPE,
    IMAGE_MIME_BY_EXTENSION,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_SOURCE_BYTES,
    ImageProcessingError,
    compress_image_data,
    inspect_image_dimensions,
    read_image_source_bytes,
    wire_image_media_type,
)
from chrys.foundation.text.images import (
    MAX_IMAGE_PIXELS as MAX_IMAGE_PIXELS,
)
from chrys.foundation.text.mentions import MentionToken, iter_mention_tokens
from chrys.kernel import Content

_ATTACHMENT_ERROR = msg(
    "attachments.attachment_error",
    fallback="We couldn't attach this image.\n\n{items}",
    plural_fallback="We couldn't attach these images.\n\n{items}",
    multiline=True,
)
_VISION_UNSUPPORTED = msg(
    "attachments.vision_unsupported",
    fallback=(
        'The active model profile "{label}" does not support image input.\n\n'
        'Enable "Vision Model" in Models, or switch to a multimodal profile and send the message again.\n\n'
        "Image not attached:\n{files}"
    ),
    plural_fallback=(
        'The active model profile "{label}" does not support image input.\n\n'
        'Enable "Vision Model" in Models, or switch to a multimodal profile and send the message again.\n\n'
        "Images not attached:\n{files}"
    ),
    multiline=True,
)
_VISION_UNSUPPORTED_UNNAMED = msg(
    "attachments.vision_unsupported_unnamed",
    fallback=(
        "The active model profile does not support image input.\n\n"
        'Enable "Vision Model" in Models, or switch to a multimodal profile and send the message again.\n\n'
        "Image not attached:\n{files}"
    ),
    plural_fallback=(
        "The active model profile does not support image input.\n\n"
        'Enable "Vision Model" in Models, or switch to a multimodal profile and send the message again.\n\n'
        "Images not attached:\n{files}"
    ),
    multiline=True,
)
_RETRY_IMAGE_UNSUPPORTED = msg(
    "attachments.retry_image_unsupported",
    fallback=(
        "Images cannot be attached to retry or continuation prompts.\n\n"
        "Send the image in a new message after this retry or continuation finishes.\n\n"
        "Image not attached:\n{files}"
    ),
    plural_fallback=(
        "Images cannot be attached to retry or continuation prompts.\n\n"
        "Send the image in a new message after this retry or continuation finishes.\n\n"
        "Images not attached:\n{files}"
    ),
    multiline=True,
)
_HISTORY_VISION_UNSUPPORTED = msg(
    "attachments.history_vision_unsupported",
    fallback=(
        'The active model profile "{label}" does not support image input.\n\n'
        "This session already contains image attachments in its active history. "
        "Switch to a multimodal profile, or start a new text-only session before continuing."
    ),
    multiline=True,
)
_HISTORY_VISION_UNSUPPORTED_UNNAMED = msg(
    "attachments.history_vision_unsupported_unnamed",
    fallback=(
        "The active model profile does not support image input.\n\n"
        "This session already contains image attachments in its active history. "
        "Switch to a multimodal profile, or start a new text-only session before continuing."
    ),
    multiline=True,
)


@dataclass(frozen=True)
class ImageAttachment:
    """Image bytes resolved from a user-authored ``@file`` mention."""

    path: Path
    mention: str
    media_type: str
    data: bytes
    size: int
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class ImageReference:
    """A supported image-looking mention resolved without touching file bytes."""

    path: Path
    mention: str
    media_type: str


@dataclass(frozen=True)
class ImageMention(ImageReference):
    """A supported image mention resolved without reading file bytes."""

    size: int


@dataclass(frozen=True)
class AttachmentParseResult:
    """Result of parsing multimodal user-message attachments."""

    attachments: list[ImageAttachment]
    errors: list[str]


@dataclass(frozen=True)
class AttachmentDiscoveryResult:
    """Result of discovering image mentions without loading image bytes."""

    mentions: list[ImageMention]
    errors: list[str]


class _ImageReference(Protocol):
    @property
    def path(self) -> Path: ...


def discover_image_mentions(text: str, cwd: Path) -> AttachmentDiscoveryResult:
    """Discover supported image ``@file`` mentions from *text* without reading bytes.

    Non-image mentions are ignored so existing text-only ``@README.md``
    behavior remains unchanged. Image-looking mentions that cannot be loaded
    are returned as errors to avoid silently sending only a filename.
    """
    mentions: list[ImageMention] = []
    errors: list[str] = []

    for reference in discover_image_references(text, cwd):
        try:
            path = reference.path.expanduser()
        except RuntimeError as exc:
            errors.append(_format_image_error(reference.mention, reference.path, f"This image path is invalid: {exc}"))
            continue
        try:
            resolved = path.resolve(strict=False)
        except ValueError as exc:
            errors.append(_format_image_error(reference.mention, path, f"This image path is invalid: {exc}"))
            continue
        try:
            stat_result = resolved.stat()
        except ValueError as exc:
            errors.append(_format_image_error(reference.mention, resolved, f"This image path is invalid: {exc}"))
            continue
        except OSError as exc:
            errors.append(
                _format_image_error(
                    reference.mention, resolved, f"{APP_DISPLAY_NAME} couldn't access this image file: {exc}"
                )
            )
            continue
        if not stat_module.S_ISREG(stat_result.st_mode):
            errors.append(_format_image_error(reference.mention, resolved, "This path is not a regular file."))
            continue
        if stat_result.st_size > MAX_IMAGE_SOURCE_BYTES:
            errors.append(
                _format_image_error(
                    reference.mention,
                    resolved,
                    _format_source_too_large_reason(stat_result.st_size),
                )
            )
            continue
        mentions.append(
            ImageMention(
                path=resolved,
                mention=reference.mention,
                media_type=reference.media_type,
                size=stat_result.st_size,
            )
        )

    return AttachmentDiscoveryResult(mentions=mentions, errors=errors)


def discover_image_references(text: str, cwd: Path) -> list[ImageReference]:
    """Discover supported image-looking ``@file`` mentions without filesystem access."""
    return [
        ImageReference(path=path, mention=token.mention, media_type=media_type)
        for token, path, media_type in _iter_image_reference_tokens(text, cwd)
    ]


def replace_image_mentions_with_paths(text: str, cwd: Path) -> str:
    """Return *text* with supported image ``@file`` mentions replaced by plain paths."""
    body, tail = selections_span(text)
    pieces: list[str] = []
    last_end = 0
    changed = False
    for token, path, _media_type in _iter_image_reference_tokens(text, cwd):
        pieces.append(body[last_end : token.start])
        pieces.append(str(path))
        last_end = token.end
        changed = True

    if not changed:
        return text
    pieces.append(body[last_end:])
    pieces.append(tail)
    return "".join(pieces)


def _iter_image_reference_tokens(text: str, cwd: Path) -> list[tuple[MentionToken, Path, str]]:
    """Return supported image-looking mention tokens plus their resolved paths."""
    token_refs: list[tuple[MentionToken, Path, str]] = []
    try:
        root = cwd.expanduser()
    except RuntimeError:
        root = cwd

    # A trailing selections block is quoted conversation text, not authored
    # input: its mentions are content under discussion, never attachments.
    body, _tail = selections_span(text)
    for token in iter_mention_tokens(body):
        if _is_folder_style_mention_value(token.value):
            continue
        try:
            raw_path = Path(token.value).expanduser()
        except RuntimeError:
            media_type = IMAGE_MIME_BY_EXTENSION.get(Path(token.value).suffix.lower())
            if media_type is not None:
                token_refs.append((token, Path(token.value), media_type))
            continue
        path = raw_path if raw_path.is_absolute() else root / raw_path
        media_type = IMAGE_MIME_BY_EXTENSION.get(path.suffix.lower())
        if media_type is None:
            continue
        token_refs.append((token, path, media_type))

    return token_refs


def _is_folder_style_mention_value(value: str) -> bool:
    """Return whether an ``@`` mention was written as a directory reference."""
    return value.endswith(("/", "\\"))


def load_image_attachments(mentions: Sequence[ImageMention]) -> AttachmentParseResult:
    """Load image bytes for previously discovered mentions."""
    attachments: list[ImageAttachment] = []
    errors: list[str] = []

    for mention in mentions:
        try:
            stat_result = mention.path.stat()
        except ValueError as exc:
            errors.append(_format_image_error(mention.mention, mention.path, f"This image path is invalid: {exc}"))
            continue
        except OSError as exc:
            errors.append(
                _format_image_error(
                    mention.mention, mention.path, f"{APP_DISPLAY_NAME} couldn't access this image file: {exc}"
                )
            )
            continue
        if not stat_module.S_ISREG(stat_result.st_mode):
            errors.append(_format_image_error(mention.mention, mention.path, "This path is not a regular file."))
            continue
        if stat_result.st_size > MAX_IMAGE_SOURCE_BYTES:
            errors.append(
                _format_image_error(
                    mention.mention,
                    mention.path,
                    _format_source_too_large_reason(stat_result.st_size),
                )
            )
            continue
        try:
            data = read_image_source_bytes(mention.path, max_bytes=MAX_IMAGE_SOURCE_BYTES)
        except ValueError as exc:
            errors.append(_format_image_error(mention.mention, mention.path, f"This image path is invalid: {exc}"))
            continue
        except OSError as exc:
            errors.append(
                _format_image_error(
                    mention.mention, mention.path, f"{APP_DISPLAY_NAME} couldn't read this image file: {exc}"
                )
            )
            continue
        if len(data) > MAX_IMAGE_SOURCE_BYTES:
            errors.append(
                _format_image_error(
                    mention.mention,
                    mention.path,
                    _format_source_too_large_reason(len(data)),
                )
            )
            continue
        # The bytes name the type, not the file name. Model APIs read only PNG,
        # JPEG, GIF and WebP: other bytes behind an image name (a renamed BMP)
        # are converted like an oversized image.
        media_type = wire_image_media_type(data, None)
        if len(data) > MAX_IMAGE_BYTES or media_type is None:
            try:
                data = compress_image_data(data, max_bytes=MAX_IMAGE_BYTES)
            except ImageProcessingError as exc:
                errors.append(_format_image_error(mention.mention, mention.path, str(exc)))
                continue
            media_type = COMPRESSED_IMAGE_MEDIA_TYPE
        dimensions = _image_dimensions(data)
        width, height = dimensions if dimensions is not None else (None, None)
        attachments.append(
            ImageAttachment(
                path=mention.path,
                mention=mention.mention,
                media_type=media_type,
                data=data,
                size=len(data),
                width=width,
                height=height,
            )
        )

    return AttachmentParseResult(attachments=attachments, errors=errors)


def parse_image_mentions(text: str, cwd: Path) -> AttachmentParseResult:
    """Parse and load supported image ``@file`` mentions from *text*."""
    discovered = discover_image_mentions(text, cwd)
    if discovered.errors:
        return AttachmentParseResult(attachments=[], errors=discovered.errors)
    return load_image_attachments(discovered.mentions)


def build_user_contents(text: str, attachments: list[ImageAttachment]) -> list[Any]:
    """Build framework user-message contents from text plus image attachments."""
    if not attachments:
        return [text]
    contents: list[Any] = [text]
    contents.extend(
        Content.from_data(
            data=attachment.data,
            media_type=attachment.media_type,
            additional_properties=_image_additional_properties(attachment),
        )
        for attachment in attachments
    )
    return contents


def _image_dimensions(data: bytes) -> tuple[int, int] | None:
    try:
        return inspect_image_dimensions(data)
    except ImageProcessingError:
        return None


def _image_additional_properties(attachment: ImageAttachment) -> dict[str, Any]:
    properties: dict[str, Any] = {"media_type": attachment.media_type}
    if attachment.width is not None and attachment.height is not None:
        properties["width"] = attachment.width
        properties["height"] = attachment.height
    return properties


def format_attachment_error_message(errors: list[str]) -> str:
    """Return a user-facing attachment load error message."""
    if not errors:
        return ""
    joined = "\n".join(f"- {error}" for error in errors)
    heading = "We couldn't attach this image." if len(errors) == 1 else "We couldn't attach these images."
    return f"{heading}\n\n{joined}"


def attachment_error_display(errors: list[str]) -> MessageRef:
    """Return the localized display reference for attachment load errors."""
    items = DisplayBlock("\n".join(f"- {error}" for error in errors))
    return _ATTACHMENT_ERROR.bind(count=len(errors), items=items)


def _format_source_too_large_reason(size: int) -> str:
    """Return friendly copy for images above the auto-compression source cap."""
    return (
        f"This image is {_format_mb(size, round_up=True)}. {APP_DISPLAY_NAME} can automatically compress images up to "
        f"{_format_mb(MAX_IMAGE_SOURCE_BYTES)} before sending them. "
        "Resize it or export a smaller copy, then try again."
    )


def _format_image_error(mention: str, path: Path, reason: str) -> str:
    """Format an image attachment error without repeating absolute pasted paths."""
    rendered_path = str(path)
    if rendered_path in mention or _mention_path_is_absolute(mention):
        return f"{mention}: {reason}"
    return f"{mention}: {reason}: {rendered_path}"


def _mention_path_is_absolute(mention: str) -> bool:
    """Return True when an ``@`` mention already contains an absolute path."""
    if not mention.startswith("@"):
        return False
    value = mention[1:]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    try:
        return Path(value).expanduser().is_absolute()
    except RuntimeError, ValueError:
        return False


def _format_mb(size_bytes: int, *, round_up: bool = False) -> str:
    """Format a byte count as user-facing megabytes."""
    value = size_bytes / (1024 * 1024)
    if round_up:
        value = math.ceil(value * 10) / 10
    return f"{value:.1f} MB"


def format_vision_unsupported_message(model_name: str, images: Sequence[_ImageReference]) -> str:
    """Return a consistent user-facing message for text-only active models."""
    profile = _format_model_profile_subject(model_name)
    files = _format_image_path_list(images)
    return (
        f"{profile} does not support image input.\n\n"
        'Enable "Vision Model" in Models, or switch to a multimodal profile and send the message again.\n\n'
        f"{_image_list_heading(images)}:\n{files}"
    )


def vision_unsupported_display(model_name: str, images: Sequence[_ImageReference]) -> MessageRef:
    """Return the localized display reference for text-only active models."""
    label = model_name.strip()
    files = DisplayBlock(_format_image_path_list(images))
    if not label:
        return _VISION_UNSUPPORTED_UNNAMED.bind(count=len(images), files=files)
    return _VISION_UNSUPPORTED.bind(label=label, count=len(images), files=files)


def format_retry_image_unsupported_message(images: Sequence[_ImageReference]) -> str:
    """Return a user-facing message for retry notes containing image mentions."""
    files = _format_image_path_list(images)
    return (
        "Images cannot be attached to retry or continuation prompts.\n\n"
        "Send the image in a new message after this retry or continuation finishes.\n\n"
        f"{_image_list_heading(images)}:\n{files}"
    )


def retry_image_unsupported_display(images: Sequence[_ImageReference]) -> MessageRef:
    """Return the localized display reference for retry image mentions."""
    return _RETRY_IMAGE_UNSUPPORTED.bind(
        count=len(images),
        files=DisplayBlock(_format_image_path_list(images)),
    )


def format_history_vision_unsupported_message(model_name: str) -> str:
    """Return a user-facing message when active history already contains image data."""
    profile = _format_model_profile_subject(model_name)
    return (
        f"{profile} does not support image input.\n\n"
        "This session already contains image attachments in its active history. "
        "Switch to a multimodal profile, or start a new text-only session before continuing."
    )


def history_vision_unsupported_display(model_name: str) -> MessageRef:
    """Return the localized display reference for image-bearing active history."""
    label = model_name.strip()
    if not label:
        return _HISTORY_VISION_UNSUPPORTED_UNNAMED.bind()
    return _HISTORY_VISION_UNSUPPORTED.bind(label=label)


def _format_model_profile_subject(model_name: str) -> str:
    """Return the subject phrase used in image-input rejection dialogs."""
    label = model_name.strip()
    if not label:
        return "The active model profile"
    return f'The active model profile "{label}"'


def _format_image_path_list(images: Sequence[_ImageReference]) -> str:
    """Return a simple bullet list of image paths for modal bodies."""
    return "\n".join(f"- {image.path}" for image in images)


def _image_list_heading(images: Sequence[_ImageReference]) -> str:
    """Return a heading that matches the number of rejected image paths."""
    return "Image not attached" if len(images) == 1 else "Images not attached"
