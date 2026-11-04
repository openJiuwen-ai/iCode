# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Prompt content preparation and image-history policy for main-agent turns."""

from __future__ import annotations

import asyncio
import base64
import binascii
import functools
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from chrys.foundation.events.types import (
    Error,
    ImageAttachmentCompressionFinished,
    ImageAttachmentCompressionStarted,
    Warning,
)
from chrys.foundation.i18n import msg
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.platform import safe_getcwd
from chrys.foundation.text.images import LoadedImage, shrink_image_data
from chrys.orchestration.engine.run.attachments import (
    MAX_IMAGE_BYTES,
    AttachmentDiscoveryResult,
    AttachmentParseResult,
    ImageMention,
    ImageReference,
    attachment_error_display,
    build_user_contents,
    discover_image_mentions,
    discover_image_references,
    format_attachment_error_message,
    format_history_vision_unsupported_message,
    format_retry_image_unsupported_message,
    format_vision_unsupported_message,
    history_vision_unsupported_display,
    load_image_attachments,
    retry_image_unsupported_display,
    vision_unsupported_display,
)
from chrys.orchestration.engine.state.machine import EngineState

if TYPE_CHECKING:
    from chrys.foundation.events.bus import EventBus
    from chrys.orchestration.engine.state.active_session import ActiveSession
    from chrys.orchestration.engine.state.current_agent import CurrentAgent
    from chrys.orchestration.engine.state.machine import EngineStateMachine
    from chrys.service.session.history import SessionHistoryManager

logger = logging.getLogger(__name__)

_IMAGE_COMPRESSION_TIMEOUT_SECONDS = 30.0
_PROMPT_CONTENT_IMAGE_ATTACHMENT_WHILE_RUNNING = msg(
    "prompt_content.image_attachment_while_running",
    fallback=(
        "Images cannot be added while the agent is responding.\n\n"
        "Wait for the current response to finish, then send the image in a new message."
    ),
    multiline=True,
)

ImageMentionDiscoverer = Callable[[str, Path], AttachmentDiscoveryResult]
ImageReferenceDiscoverer = Callable[[str, Path], list[ImageReference]]


class ImageAttachmentLoader(Protocol):
    """Loads discovered image mentions, each compressed to at most *max_bytes*."""

    def __call__(self, mentions: Sequence[ImageMention], *, max_bytes: int) -> AttachmentParseResult: ...


PromptPublicationGuard = Callable[[], bool]


@dataclass(frozen=True)
class PromptContentResult:
    """Prepared user contents for a fresh prompt."""

    contents: list[Any]


class PromptContentPreparer:
    """Prepare fresh prompt contents and reject unsupported image usage."""

    def __init__(
        self,
        *,
        session: ActiveSession,
        current: CurrentAgent,
        bus: EventBus,
        history: SessionHistoryManager,
        fsm: EngineStateMachine,
        discover_mentions: ImageMentionDiscoverer = discover_image_mentions,
        discover_references: ImageReferenceDiscoverer = discover_image_references,
        load_attachments: ImageAttachmentLoader = load_image_attachments,
        compression_timeout_seconds: float = _IMAGE_COMPRESSION_TIMEOUT_SECONDS,
        image_send_limit: Callable[[], int] = lambda: MAX_IMAGE_BYTES,
    ) -> None:
        self._session = session
        self._current = current
        self._bus = bus
        self._discover_mentions = discover_mentions
        self._discover_references = discover_references
        self._load_attachments = load_attachments
        self._compression_timeout_seconds = compression_timeout_seconds
        self._image_send_limit = image_send_limit
        self._history = history
        self._history_policy = ImageHistoryPolicy(history=history, fsm=fsm)

    async def prepare_fresh(
        self,
        text: str,
        *,
        discovered: AttachmentDiscoveryResult | None = None,
        event_session_id: str | None = None,
        should_publish: PromptPublicationGuard | None = None,
    ) -> PromptContentResult | None:
        """Return prepared user-message contents or publish a recoverable attachment error."""
        event_session_id = self._session.session_id if event_session_id is None else event_session_id
        if not self._current.manifest.runtime_details.model.vision:
            image_references = self._discover_references(text, self.attachment_cwd())
            if image_references:
                await self.publish_current_prompt_vision_rejection(
                    image_references,
                    session_id=event_session_id,
                    should_publish=should_publish,
                )
                return None
            if self._history_policy.active_history_has_image_content():
                await self.publish_history_vision_rejection(
                    session_id=event_session_id,
                    should_publish=should_publish,
                )
                return None
        if discovered is None:
            discovered = self.inspect_image_mentions(text)
        if discovered.errors:
            if self._should_publish(should_publish):
                await self._bus.publish(
                    Error(
                        code="image_attachment_error",
                        message=format_attachment_error_message(discovered.errors),
                        display_message=attachment_error_display(discovered.errors),
                        session_id=event_session_id,
                    )
                )
            return None
        if not discovered.mentions:
            return PromptContentResult(contents=build_user_contents(text, []))

        max_bytes = self._image_send_limit()
        load = functools.partial(self._load_attachments, discovered.mentions, max_bytes=max_bytes)
        compression_count = sum(1 for mention in discovered.mentions if mention.size > max_bytes)
        timeout_errors: list[str] = []
        loaded = None
        compression_started = False
        if compression_count and self._should_publish(should_publish):
            await self._bus.publish(
                ImageAttachmentCompressionStarted(image_count=compression_count, session_id=event_session_id)
            )
            compression_started = True
        try:
            if compression_count:
                try:
                    loaded = await asyncio.wait_for(
                        asyncio.to_thread(load),
                        timeout=self._compression_timeout_seconds,
                    )
                except TimeoutError:
                    timeout_errors = self.format_image_preparation_timeout_errors(discovered.mentions)
            else:
                loaded = await asyncio.to_thread(load)
        finally:
            if compression_count and (compression_started or self._should_publish(should_publish)):
                await self._bus.publish(
                    ImageAttachmentCompressionFinished(image_count=compression_count, session_id=event_session_id)
                )
        if timeout_errors:
            if self._should_publish(should_publish):
                await self._bus.publish(
                    Error(
                        code="image_attachment_error",
                        message=format_attachment_error_message(timeout_errors),
                        display_message=attachment_error_display(timeout_errors),
                        session_id=event_session_id,
                    )
                )
            return None
        if loaded is None:
            raise RuntimeError("Image attachment loading did not produce a result.")
        if loaded.errors:
            if self._should_publish(should_publish):
                await self._bus.publish(
                    Error(
                        code="image_attachment_error",
                        message=format_attachment_error_message(loaded.errors),
                        display_message=attachment_error_display(loaded.errors),
                        session_id=event_session_id,
                    )
                )
            return None
        return PromptContentResult(contents=build_user_contents(text, loaded.attachments))

    async def reject_text_only_prepared_contents(
        self,
        text: str,
        contents: list[Any],
        *,
        event_session_id: str | None = None,
        should_publish: PromptPublicationGuard | None = None,
    ) -> bool:
        """Publish a rejection if prebuilt contents cannot run on a text-only model."""
        event_session_id = self._session.session_id if event_session_id is None else event_session_id
        if self._current.manifest.runtime_details.model.vision:
            return False
        image_references = self._discover_references(text, self.attachment_cwd())
        if image_references:
            await self.publish_current_prompt_vision_rejection(
                image_references,
                session_id=event_session_id,
                should_publish=should_publish,
            )
            return True
        if self._history_policy.contents_have_image_content(contents):
            await self.publish_history_vision_rejection(
                session_id=event_session_id,
                should_publish=should_publish,
            )
            return True
        if self._history_policy.active_history_has_image_content():
            await self.publish_history_vision_rejection(
                session_id=event_session_id,
                should_publish=should_publish,
            )
            return True
        return False

    def inspect_image_mentions(self, text: str) -> AttachmentDiscoveryResult:
        """Discover image mentions for injection/retry rejection without reading bytes."""
        return self._discover_mentions(text, self.attachment_cwd())

    async def reject_injected_images(self, text: str, *, session_id: str | None = None) -> bool:
        """Publish a non-fatal rejection for image mentions while a run is active."""
        return await self.publish_injected_attachment_rejection(
            self.inspect_image_mentions(text),
            session_id=session_id,
        )

    async def reject_retry_images(self, text: str, *, session_id: str | None = None) -> bool:
        """Publish a non-fatal rejection for retry notes containing image mentions."""
        return await self.publish_retry_attachment_rejection(
            self.inspect_image_mentions(text),
            session_id=session_id,
        )

    async def reject_text_only_active_history(self) -> bool:
        """Publish a text-only model rejection when active history contains image content."""
        if self._current.manifest.runtime_details.model.vision:
            return False
        if not self._history_policy.active_history_has_image_content(is_retry=True):
            return False
        await self.publish_history_vision_rejection()
        return True

    async def publish_current_prompt_vision_rejection(
        self,
        images: list[Any],
        *,
        session_id: str | None = None,
        should_publish: PromptPublicationGuard | None = None,
    ) -> None:
        """Reject current prompt image references for a text-only model."""
        if not self._should_publish(should_publish):
            return
        await self._bus.publish(
            Error(
                code="vision_unsupported",
                message=format_vision_unsupported_message(self._current.manifest.runtime_details.model.name, images),
                display_message=vision_unsupported_display(self._current.manifest.runtime_details.model.name, images),
                session_id=self._session.session_id if session_id is None else session_id,
            )
        )

    async def publish_history_vision_rejection(
        self,
        *,
        session_id: str | None = None,
        should_publish: PromptPublicationGuard | None = None,
    ) -> None:
        """Reject a text-only model before replaying historical image data."""
        if not self._should_publish(should_publish):
            return
        await self._bus.publish(
            Error(
                code="vision_unsupported",
                message=format_history_vision_unsupported_message(self._current.manifest.runtime_details.model.name),
                display_message=history_vision_unsupported_display(self._current.manifest.runtime_details.model.name),
                session_id=self._session.session_id if session_id is None else session_id,
            )
        )

    async def publish_injected_attachment_rejection(
        self,
        discovered: AttachmentDiscoveryResult,
        *,
        session_id: str | None = None,
    ) -> bool:
        """Publish a non-fatal rejection for image mentions while a run is active."""
        event_session_id = self._session.session_id if session_id is None else session_id
        if discovered.errors:
            await self._bus.publish(
                Warning(
                    code="image_attachment_error",
                    message=format_attachment_error_message(discovered.errors),
                    display_message=attachment_error_display(discovered.errors),
                    session_id=event_session_id,
                )
            )
            return True
        if not discovered.mentions:
            return False
        await self._bus.publish(
            Warning(
                code="image_attachment_while_running",
                message=(
                    "Images cannot be added while the agent is responding.\n\n"
                    "Wait for the current response to finish, then send the image in a new message."
                ),
                display_message=_PROMPT_CONTENT_IMAGE_ATTACHMENT_WHILE_RUNNING.bind(),
                session_id=event_session_id,
            )
        )
        return True

    async def publish_retry_attachment_rejection(
        self,
        discovered: AttachmentDiscoveryResult,
        *,
        session_id: str | None = None,
    ) -> bool:
        """Publish a non-fatal rejection for retry notes containing image mentions."""
        event_session_id = self._session.session_id if session_id is None else session_id
        if discovered.errors:
            await self._bus.publish(
                Warning(
                    code="image_attachment_error",
                    message=format_attachment_error_message(discovered.errors),
                    display_message=attachment_error_display(discovered.errors),
                    session_id=event_session_id,
                )
            )
            return True
        if not discovered.mentions:
            return False
        await self._bus.publish(
            Warning(
                code="image_attachment_retry_unsupported",
                message=format_retry_image_unsupported_message(discovered.mentions),
                display_message=retry_image_unsupported_display(discovered.mentions),
                session_id=event_session_id,
            )
        )
        return True

    def attachment_cwd(self) -> Path:
        """Return the cwd used to resolve user-authored ``@file`` mentions."""
        if self._session.workspace is not None:
            return Path(self._session.workspace.primary_cwd)
        return Path(safe_getcwd())

    def format_image_preparation_timeout_errors(self, mentions: list[Any]) -> list[str]:
        """Return attachment errors for a compression/preparation timeout."""
        return format_image_preparation_timeout_errors(mentions, self._compression_timeout_seconds)

    def format_image_compression_timeout_duration(self) -> str:
        """Return the user-facing compression timeout duration."""
        return format_image_compression_timeout_duration(self._compression_timeout_seconds)

    async def shrink_history_images(self) -> None:
        """Shrink, in place, every image of the session history over the send limit.

        After the limit is lowered, the next turn or retry sends the session's
        earlier images within it too, so a request a gateway refused as too
        large goes through; the turn's save keeps them shrunk. Shrinking stops
        at the attachment compression timeout, keeping the images it finished:
        a later turn or retry goes on with the rest.
        """
        max_bytes = self._image_send_limit()
        oversized = self._history.images_over(max_bytes)
        if not oversized:
            return
        count = len(oversized)
        written = 0
        session_id = self._session.session_id
        await self._bus.publish(ImageAttachmentCompressionStarted(image_count=count, session_id=session_id))
        try:
            async with asyncio.timeout(self._compression_timeout_seconds):
                for image in oversized:
                    # Off the event loop, on a copy of the payload: the image
                    # changes only once its replacement is ready, never halfway.
                    replacement = await asyncio.to_thread(_shrink_image, image.uri, max_bytes)
                    if replacement is not None:
                        written += self._history.shrink_images([(image, replacement)])
        except TimeoutError:
            pass
        finally:
            await self._bus.publish(ImageAttachmentCompressionFinished(image_count=count, session_id=session_id))
        if written < count:
            logger.warning("%d history image(s) stay over the %d-byte send limit.", count - written, max_bytes)

    @staticmethod
    def _should_publish(guard: PromptPublicationGuard | None) -> bool:
        """Return whether prompt-preparation side effects still belong to the live owner."""
        return guard is None or guard()


class ImageHistoryPolicy:
    """Image-content checks over replayable session history."""

    def __init__(
        self,
        *,
        history: SessionHistoryManager,
        fsm: EngineStateMachine,
    ) -> None:
        self._history = history
        self._fsm = fsm

    def active_history_has_image_content(self, *, is_retry: bool = False) -> bool:
        """Return True if active, replayable chat history contains image content."""
        # Retry retains the opener; only a fresh submit replaces an orphaned
        # prompt during failed-turn cleanup.
        messages = self._history.messages if is_retry else self.replayable_history_messages()
        for message in messages:
            if message.additional_properties.get("_excluded"):
                continue
            if any(self.content_is_image(content) for content in message.contents):
                return True
        return False

    def replayable_history_messages(self) -> list[Any]:
        """Return the history shape a new turn will replay after pending cleanup."""
        messages = list(self._history.messages)
        if not self.pending_failed_turn_cleanup_would_run(messages):
            return messages
        while messages:
            chrys_kind = messages[-1].additional_properties.get(HistoryMarkerKind.KEY)
            if chrys_kind in HistoryMarkerKind.SESSION_COUNT_EXCLUDED:
                messages.pop()
                continue
            break
        if messages and messages[-1].role == "user":
            messages.pop()
        return messages

    def pending_failed_turn_cleanup_would_run(self, messages: list[Any]) -> bool:
        """Mirror the failed/interrupted cleanup gate without mutating history."""
        return self._fsm.state in (
            EngineState.INTERRUPTED,
            EngineState.FAILED,
        ) or self.messages_have_trailing_status_marker(messages)

    @staticmethod
    def messages_have_trailing_status_marker(messages: list[Any]) -> bool:
        """Return True when trailing history carries a status marker, skipping turn markers."""
        for message in reversed(messages):
            chrys_kind = message.additional_properties.get(HistoryMarkerKind.KEY)
            if chrys_kind == HistoryMarkerKind.TURN:
                continue
            return chrys_kind in HistoryMarkerKind.STATUS_MARKERS
        return False

    @staticmethod
    def content_is_image(content: Any) -> bool:
        """Return True for framework content blocks that carry image data/URIs."""
        media_type = getattr(content, "media_type", "") or ""
        return isinstance(media_type, str) and media_type.startswith("image/")

    def contents_have_image_content(self, contents: list[Any]) -> bool:
        """Return True when a pending user-message content list includes image content."""
        return contents_have_image_content(contents)


def _shrink_image(uri: str, max_bytes: int) -> LoadedImage | None:
    """The JPEG within *max_bytes* an image data URI shrinks to, or None when it can't."""
    header, _, encoded = uri.partition(",")
    if ";base64" not in header.lower():
        return None
    try:
        data = base64.b64decode(encoded, validate=False)
    except binascii.Error, ValueError:
        return None
    try:
        return shrink_image_data(data, max_bytes=max_bytes)
    except Exception:
        # Whatever a stored image does to the decoder, it fails no turn: it is
        # sent as it is, as one that can't be shrunk at all.
        logger.warning("A history image could not be shrunk.", exc_info=True)
        return None


def contents_have_image_content(contents: list[Any]) -> bool:
    """Return True when a pending user-message content list includes image content."""
    return any(not isinstance(content, str) and ImageHistoryPolicy.content_is_image(content) for content in contents)


def format_image_preparation_timeout_errors(mentions: list[Any], timeout_seconds: float) -> list[str]:
    """Return attachment errors for a compression/preparation timeout."""
    reason = (
        f"Image preparation took longer than {format_image_compression_timeout_duration(timeout_seconds)}. "
        "Resize oversized images or send fewer images, then try again."
    )
    return [f"{mention.mention}: {reason}" for mention in mentions]


def format_image_compression_timeout_duration(timeout_seconds: float) -> str:
    """Return the user-facing compression timeout duration."""
    seconds = max(1, int(timeout_seconds + 0.999))
    unit = "second" if seconds == 1 else "seconds"
    return f"{seconds} {unit}"
