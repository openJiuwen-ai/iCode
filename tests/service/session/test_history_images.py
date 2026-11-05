# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The history's images over a send limit, and shrinking them in place."""

from __future__ import annotations

from typing import Any

from chrys.foundation.text.images import LoadedImage
from chrys.kernel import GROUP_ANNOTATION_KEY, Content, Message
from chrys.kernel.compaction import GROUP_TOKEN_COUNT_KEY, GROUP_TOKEN_ESTIMATOR_VERSION_KEY
from chrys.service.session.history import SessionHistoryManager
from tests.support.images import image_bytes

_LIMIT = 1024


def _image(byte_count: int, **properties: Any) -> Content:
    # Only its length is read: a PNG padded out to *byte_count* bytes.
    data = image_bytes("PNG")
    return Content.from_data(data + b"\0" * (byte_count - len(data)), "image/png", additional_properties=properties)


def _bound(messages: list[Message]) -> SessionHistoryManager:
    history = SessionHistoryManager()
    history.bind({"messages": messages})
    return history


def _tool_result(*items: Content) -> Message:
    return Message("tool", [Content.from_function_result("call-1", result=list(items))])


def test_images_over_the_limit_are_those_of_user_messages_and_tool_results() -> None:
    attached = _image(_LIMIT + 1)
    viewed = _image(_LIMIT + 1)
    messages = [
        Message("user", ["look", attached, _image(_LIMIT)]),
        Message("assistant", [_image(_LIMIT + 1)]),
        _tool_result(Content.from_text("an image"), viewed, _image(_LIMIT)),
        Message("user", [Content.from_uri("https://example.com/shot.png", media_type="image/png")]),
    ]

    found = _bound(messages).images_over(_LIMIT)

    assert [(image.message, image.content) for image in found] == [(messages[0], attached), (messages[2], viewed)]
    assert [image.uri for image in found] == [attached.uri, viewed.uri]


def test_a_shrunk_image_is_written_into_the_same_content() -> None:
    viewed = _image(_LIMIT + 1, width=640, height=480, media_type="image/png", size=_LIMIT + 1, source_path="s.png")
    message = _tool_result(viewed)
    message.additional_properties[GROUP_ANNOTATION_KEY] = {
        "group_id": "g1",
        GROUP_TOKEN_COUNT_KEY: 900,
        GROUP_TOKEN_ESTIMATOR_VERSION_KEY: 1,
    }
    history = _bound([message])
    (oversized,) = history.images_over(_LIMIT)
    shrunk = LoadedImage(data=image_bytes("JPEG"), media_type="image/jpeg", width=2, height=2)

    assert history.shrink_images([(oversized, shrunk)]) == 1

    (item,) = message.contents[0].items or ()
    assert item is viewed
    assert viewed.media_type == "image/jpeg"
    assert viewed.uri is not None
    assert viewed.uri.startswith("data:image/jpeg;base64,")
    assert viewed.additional_properties == {
        "width": 2,
        "height": 2,
        "media_type": "image/jpeg",
        "size": shrunk.size,
        "source_path": "s.png",
    }
    # The estimate counted the larger image; the rest of the annotation stays.
    assert message.additional_properties[GROUP_ANNOTATION_KEY] == {"group_id": "g1"}
    assert history.images_over(_LIMIT) == []


def test_an_attachment_gains_only_the_properties_it_had() -> None:
    attached = _image(_LIMIT + 1, media_type="image/png")
    history = _bound([Message("user", ["look", attached])])
    (oversized,) = history.images_over(_LIMIT)

    history.shrink_images([(oversized, LoadedImage(data=b"\xff\xd8\xff", media_type="image/jpeg", width=3, height=1))])

    assert attached.additional_properties == {"media_type": "image/jpeg", "width": 3, "height": 1}


def test_an_image_changed_since_it_was_read_is_left_alone() -> None:
    attached = _image(_LIMIT + 1)
    history = _bound([Message("user", ["look", attached])])
    (oversized,) = history.images_over(_LIMIT)
    attached.uri = f"{attached.uri}"[:-4] + "AAAA"
    changed = attached.uri

    written = history.shrink_images(
        [(oversized, LoadedImage(data=b"\xff\xd8\xff", media_type="image/jpeg", width=1, height=1))]
    )

    assert written == 0
    assert attached.uri is changed
    assert attached.media_type == "image/png"
