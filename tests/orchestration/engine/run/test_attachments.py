# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for user-message image attachment parsing."""

from __future__ import annotations

import os
import struct
import zlib
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.text.conversation_selections import Quote, compose_outgoing_text
from chrys.foundation.text.mentions import format_file_mention, iter_mention_tokens
from chrys.orchestration.engine.run import attachments as attachments_module
from chrys.orchestration.engine.run.attachments import (
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS,
    MAX_IMAGE_SOURCE_BYTES,
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
    parse_image_mentions,
    replace_image_mentions_with_paths,
    retry_image_unsupported_display,
    vision_unsupported_display,
)
from tests.support.images import image_bytes

_PNG = image_bytes()


def _write(path: Path, data: bytes = _PNG) -> Path:
    path.write_bytes(data)
    return path


def _write_sparse(path: Path, size: int) -> Path:
    with path.open("wb") as f:
        f.truncate(size)
    return path


def _write_png_with_dimensions(path: Path, width: int, height: int, *, padding_size: int = 0) -> Path:
    def chunk(kind: bytes, data: bytes) -> bytes:
        payload = kind + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    padding = chunk(b"tEXt", b"x" * padding_size) if padding_size else b""
    return _write(path, b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + padding + chunk(b"IEND", b""))


def _write_noisy_image(
    path: Path,
    image_format: str,
    *,
    size: tuple[int, int],
    min_bytes: int,
    quality: int | None = None,
) -> Path:
    image = Image.effect_noise(size, 100).convert("RGB")
    out = BytesIO()
    kwargs = {"quality": quality} if quality is not None else {}
    image.save(out, format=image_format, **kwargs)
    assert len(out.getvalue()) > min_bytes
    return _write(path, out.getvalue())


def test_iter_mention_tokens_unescapes_quoted_paths() -> None:
    tokens = iter_mention_tokens(r'look @"C:\\Users\\me\\screen \"one\".png" and @plain.png')

    assert [token.value for token in tokens] == [r'C:\Users\me\screen "one".png', "plain.png"]
    assert tokens[0].mention == r'@"C:\\Users\\me\\screen \"one\".png"'


def test_format_file_mention_escapes_backslashes_and_quotes() -> None:
    mention = format_file_mention(r'C:\Users\me\screen "one".png')

    assert mention == r'@"C:\\Users\\me\\screen \"one\".png"'
    assert iter_mention_tokens(mention)[0].value == r'C:\Users\me\screen "one".png'


def test_replace_image_mentions_with_paths_uses_plain_resolved_paths(tmp_path: Path) -> None:
    first = tmp_path / "shot.png"
    second = tmp_path / "screen two.jpg"

    text = f'inspect @{first.name} and @"{second}" but keep @notes.txt'
    replaced = replace_image_mentions_with_paths(text, cwd=tmp_path)

    assert replaced == f"inspect {first} and {second} but keep @notes.txt"


def test_parse_image_mentions_single_png(tmp_path: Path) -> None:
    image = _write(tmp_path / "shot.png")

    result = parse_image_mentions(f"look at @{image.name}", cwd=tmp_path)

    assert result.errors == []
    assert len(result.attachments) == 1
    attachment = result.attachments[0]
    assert attachment.path == image
    assert attachment.mention == "@shot.png"
    assert attachment.media_type == "image/png"
    assert attachment.data == _PNG


def test_parse_image_mentions_multiple_and_non_image_ignored(tmp_path: Path) -> None:
    first = _write(tmp_path / "a.PNG")
    second = _write(tmp_path / "b.webp", image_bytes("WEBP"))
    _write(tmp_path / "README.md", b"text")

    result = parse_image_mentions(f"compare @{first.name} @README.md @{second.name}", cwd=tmp_path)

    assert result.errors == []
    assert [attachment.path.name for attachment in result.attachments] == ["a.PNG", "b.webp"]
    assert [attachment.media_type for attachment in result.attachments] == ["image/png", "image/webp"]


def test_parse_image_mentions_quoted_path_with_spaces(tmp_path: Path) -> None:
    image = _write(tmp_path / "screen shot.jpg", image_bytes("JPEG"))

    result = parse_image_mentions('describe @"screen shot.jpg"', cwd=tmp_path)

    assert result.errors == []
    assert len(result.attachments) == 1
    assert result.attachments[0].path == image
    assert result.attachments[0].media_type == "image/jpeg"


def test_discover_image_mentions_preserves_unknown_backslash_escapes(tmp_path: Path) -> None:
    path_text = "Users\\me\\shot.png"
    image = tmp_path / "Users" / "me" / "shot.png" if os.name == "nt" else tmp_path / path_text
    image.parent.mkdir(parents=True, exist_ok=True)
    image = _write(image).resolve(strict=False)

    result = discover_image_mentions(f'describe @"{path_text}"', cwd=tmp_path)

    assert result.errors == []
    assert len(result.mentions) == 1
    assert result.mentions[0].path == image


def test_parse_image_mentions_absolute_path_and_trailing_punctuation(tmp_path: Path) -> None:
    image = _write(tmp_path / "logo.jpeg")

    result = parse_image_mentions(f"what is @{image}> please?", cwd=Path("/"))

    assert result.errors == []
    assert len(result.attachments) == 1
    assert result.attachments[0].path == image
    assert result.attachments[0].mention == f"@{image}"


def test_parse_image_mentions_not_at_word_boundary_is_ignored(tmp_path: Path) -> None:
    _write(tmp_path / "logo.png")

    result = parse_image_mentions("email me at test@logo.png", cwd=tmp_path)

    assert result.errors == []
    assert result.attachments == []


def test_parse_image_mentions_folder_style_image_suffix_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "assets.png").mkdir()

    result = parse_image_mentions("look at @assets.png/", cwd=tmp_path)

    assert result.errors == []
    assert result.attachments == []


def test_parse_image_mentions_missing_image_is_error(tmp_path: Path) -> None:
    result = parse_image_mentions("look at @missing.png", cwd=tmp_path)

    assert result.attachments == []
    assert len(result.errors) == 1
    assert "@missing.png" in result.errors[0]


def test_parse_image_mentions_malformed_image_path_is_error(tmp_path: Path) -> None:
    result = parse_image_mentions("look at @a\0.png", cwd=tmp_path)

    assert result.attachments == []
    assert len(result.errors) == 1
    assert "@a\0.png" in result.errors[0]
    assert "This image path is invalid" in result.errors[0]


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows expanduser synthesizes C:\\Users\\<name> instead of raising; the error surfaces as 'cannot access' there",
)
def test_parse_image_mentions_unknown_user_home_path_is_error(tmp_path: Path) -> None:
    result = parse_image_mentions("look at @~nosuch_chrys_user_xyz/shot.png", cwd=tmp_path)

    assert result.attachments == []
    assert len(result.errors) == 1
    assert "@~nosuch_chrys_user_xyz/shot.png" in result.errors[0]
    assert "This image path is invalid" in result.errors[0]


@pytest.mark.parametrize(
    ("name", "image_format", "size", "quality"),
    [
        ("huge.png", "PNG", (640, 640), None),
        ("huge.jpg", "JPEG", (640, 640), 95),
        ("huge.webp", "WEBP", (640, 640), 95),
    ],
)
def test_parse_image_mentions_oversize_supported_image_is_compressed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    image_format: str,
    size: tuple[int, int],
    quality: int | None,
) -> None:
    max_image_bytes = 128 * 1024
    monkeypatch.setattr(attachments_module, "MAX_IMAGE_BYTES", max_image_bytes)
    image = _write_noisy_image(
        tmp_path / name,
        image_format,
        size=size,
        min_bytes=max_image_bytes,
        quality=quality,
    )

    result = parse_image_mentions(f"look at @{name}", cwd=tmp_path)

    assert result.errors == []
    assert len(result.attachments) == 1
    attachment = result.attachments[0]
    assert attachment.path == image
    assert attachment.mention == f"@{name}"
    assert attachment.media_type == "image/jpeg"
    assert attachment.size == len(attachment.data)
    assert attachment.size <= max_image_bytes
    assert attachment.data != image.read_bytes()
    assert attachment.data.startswith(b"\xff\xd8")


def test_parse_image_mentions_converts_an_unsupported_format_behind_a_supported_name(tmp_path: Path) -> None:
    """Model APIs don't read a renamed BMP: it goes out converted, like an oversized image."""
    image = _write(tmp_path / "shot.png", image_bytes("BMP"))

    result = parse_image_mentions("look at @shot.png", cwd=tmp_path)

    assert result.errors == []
    (attachment,) = result.attachments
    assert attachment.path == image
    assert attachment.media_type == "image/jpeg"
    assert attachment.size == len(attachment.data)
    assert attachment.data.startswith(b"\xff\xd8\xff")


def test_parse_image_mentions_refuses_a_format_pillow_may_not_decode(tmp_path: Path) -> None:
    """A TIFF behind an image name never reaches Pillow's TIFF decoder."""
    _write(tmp_path / "shot.png", image_bytes("TIFF"))

    result = parse_image_mentions("look at @shot.png", cwd=tmp_path)

    assert result.attachments == []
    assert len(result.errors) == 1
    assert result.errors[0].startswith("@shot.png: This file could not be read as a supported image.")


def test_parse_image_mentions_types_an_image_by_its_bytes(tmp_path: Path) -> None:
    jpeg = image_bytes("JPEG")
    _write(tmp_path / "shot.png", jpeg)

    result = parse_image_mentions("look at @shot.png", cwd=tmp_path)

    assert result.errors == []
    (attachment,) = result.attachments
    assert (attachment.media_type, attachment.data) == ("image/jpeg", jpeg)


def test_parse_image_mentions_unreadable_image_is_error(tmp_path: Path) -> None:
    _write(tmp_path / "shot.png", b"not an image")

    result = parse_image_mentions("look at @shot.png", cwd=tmp_path)

    assert result.attachments == []
    assert len(result.errors) == 1
    assert result.errors[0].startswith("@shot.png: This file could not be read as a supported image.")


def test_parse_image_mentions_source_over_compression_limit_is_error(tmp_path: Path) -> None:
    _write_sparse(tmp_path / "too-large.png", MAX_IMAGE_SOURCE_BYTES + 1)

    result = parse_image_mentions("look at @too-large.png", cwd=tmp_path)

    assert result.attachments == []
    assert len(result.errors) == 1
    assert result.errors[0].startswith("@too-large.png: This image is 50.1 MB.")
    assert (
        f"{APP_DISPLAY_NAME} can automatically compress images up to 50.0 MB before sending them." in result.errors[0]
    )
    assert "Resize it or export a smaller copy, then try again." in result.errors[0]


def test_load_image_attachments_rejects_file_that_grows_after_stat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = _write(tmp_path / "grows.png", b"x")
    mention = ImageMention(path=image, mention="@grows.png", media_type="image/png", size=1)
    read_sizes: list[int] = []

    class _GrowingFile:
        def __enter__(self) -> _GrowingFile:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            return b"x" * size

    def open_growing_file(self: Path, mode: str = "r", *_args: object, **_kwargs: object) -> _GrowingFile:
        assert self == image
        assert mode == "rb"
        return _GrowingFile()

    monkeypatch.setattr(attachments_module, "MAX_IMAGE_SOURCE_BYTES", 8)
    monkeypatch.setattr(Path, "open", open_growing_file)

    result = attachments_module.load_image_attachments([mention])

    assert result.attachments == []
    assert len(result.errors) == 1
    assert "@grows.png: This image is" in result.errors[0]
    assert read_sizes == [9]


def test_parse_image_mentions_huge_dimensions_are_error(tmp_path: Path) -> None:
    _write_png_with_dimensions(
        tmp_path / "too-wide.png",
        MAX_IMAGE_PIXELS + 1,
        1,
        padding_size=MAX_IMAGE_BYTES + 1,
    )

    result = parse_image_mentions("look at @too-wide.png", cwd=tmp_path)

    assert result.attachments == []
    assert len(result.errors) == 1
    assert "@too-wide.png: This image is too large to process safely" in result.errors[0]
    assert "limit 80,000,000 pixels" in result.errors[0]
    assert "Resize it or export a smaller copy, then try again." in result.errors[0]


def test_parse_image_mentions_pillow_decompression_bomb_is_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = _write(tmp_path / "bomb.png", b"x" * (MAX_IMAGE_BYTES + 1))

    from PIL import Image as PILImage

    def raise_decompression_bomb(_data: object, *, formats: tuple[str, ...]) -> object:
        raise PILImage.DecompressionBombError("too many pixels")

    monkeypatch.setattr(PILImage, "open", raise_decompression_bomb)

    result = parse_image_mentions(f"look at @{image.name}", cwd=tmp_path)

    assert result.attachments == []
    assert len(result.errors) == 1
    assert "@bomb.png: This image is too large to process safely." in result.errors[0]
    assert "Resize it or export a smaller copy, then try again." in result.errors[0]


def test_parse_image_mentions_pillow_decompression_warning_is_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = _write(tmp_path / "warning.png", b"x" * (MAX_IMAGE_BYTES + 1))

    from PIL import Image as PILImage

    def raise_decompression_warning(_data: object, *, formats: tuple[str, ...]) -> object:
        raise PILImage.DecompressionBombWarning("too many pixels")

    monkeypatch.setattr(PILImage, "open", raise_decompression_warning)

    result = parse_image_mentions(f"look at @{image.name}", cwd=tmp_path)

    assert result.attachments == []
    assert len(result.errors) == 1
    assert "@warning.png: This image is too large to process safely." in result.errors[0]
    assert "Resize it or export a smaller copy, then try again." in result.errors[0]


def test_parse_image_mentions_absolute_path_error_path_is_not_repeated(tmp_path: Path) -> None:
    image = _write(tmp_path / "huge image.png", b"x" * (MAX_IMAGE_BYTES + 1)).resolve()

    result = parse_image_mentions(f'describe @"{image}"', cwd=Path("/"))

    assert result.attachments == []
    assert len(result.errors) == 1
    assert result.errors[0].count(str(image)) == 1
    assert result.errors[0].startswith(f'@"{image}": This file could not be read as a supported image.')


def test_parse_image_mentions_absolute_symlink_error_path_is_not_repeated(tmp_path: Path) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    image = _write(real_dir / "huge.png", b"x" * (MAX_IMAGE_BYTES + 1))
    link_dir = tmp_path / "link"
    try:
        link_dir.symlink_to(real_dir, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    mentioned = link_dir / image.name

    result = parse_image_mentions(f"look at @{mentioned}", cwd=Path("/"))

    assert result.attachments == []
    assert len(result.errors) == 1
    assert result.errors[0].startswith(f"@{mentioned}: This file could not be read as a supported image.")


def test_build_user_contents_text_only_round_trips() -> None:
    assert build_user_contents("hello", []) == ["hello"]


def test_build_user_contents_appends_image_content_after_text(tmp_path: Path) -> None:
    _write(tmp_path / "shot.png")
    parsed = parse_image_mentions("@shot.png", cwd=tmp_path)

    contents = build_user_contents("look at @shot.png", parsed.attachments)

    assert contents[0] == "look at @shot.png"
    assert contents[1].type == "data"
    assert contents[1].media_type == "image/png"
    assert contents[1].uri.startswith("data:image/png;base64,")


def test_build_user_contents_adds_dimensions_for_valid_png(tmp_path: Path) -> None:
    image = Image.new("RGB", (32, 16), (20, 40, 60))
    output = BytesIO()
    image.save(output, format="PNG")
    _write(tmp_path / "shot.png", output.getvalue())
    parsed = parse_image_mentions("@shot.png", cwd=tmp_path)

    contents = build_user_contents("look at @shot.png", parsed.attachments)

    assert contents[1].additional_properties["width"] == 32
    assert contents[1].additional_properties["height"] == 16
    assert contents[1].additional_properties["media_type"] == "image/png"


def test_build_user_contents_adds_dimensions_for_multiple_valid_pngs(tmp_path: Path) -> None:
    for name, size in (("first.png", (32, 16)), ("second.png", (48, 24))):
        image = Image.new("RGB", size, (20, 40, 60))
        output = BytesIO()
        image.save(output, format="PNG")
        _write(tmp_path / name, output.getvalue())
    parsed = parse_image_mentions("@first.png @second.png", cwd=tmp_path)

    contents = build_user_contents("compare @first.png @second.png", parsed.attachments)

    assert len(contents) == 3
    assert contents[1].additional_properties["width"] == 32
    assert contents[1].additional_properties["height"] == 16
    assert contents[2].additional_properties["width"] == 48
    assert contents[2].additional_properties["height"] == 24


def test_format_attachment_error_message() -> None:
    message = format_attachment_error_message(["@missing.png: nope"])

    assert message.startswith("We couldn't attach this image.")
    assert "- @missing.png: nope" in message


def test_format_attachment_error_message_plural() -> None:
    message = format_attachment_error_message(["@a.png: nope", "@b.png: nope"])

    assert message.startswith("We couldn't attach these images.")
    assert "- @a.png: nope" in message
    assert "- @b.png: nope" in message


def test_format_vision_unsupported_message_uses_modal_structure(tmp_path: Path) -> None:
    message = format_vision_unsupported_message(
        "Text Only",
        [ImageReference(path=tmp_path / "shot.png", mention="@shot.png", media_type="image/png")],
    )

    assert message.startswith('The active model profile "Text Only" does not support image input.')
    assert 'Enable "Vision Model" in Models' in message
    assert "Image not attached:\n- " in message
    assert "shot.png" in message


def test_format_retry_image_unsupported_message_uses_modal_structure(tmp_path: Path) -> None:
    message = format_retry_image_unsupported_message(
        [
            ImageReference(path=tmp_path / "a.png", mention="@a.png", media_type="image/png"),
            ImageReference(path=tmp_path / "b.jpg", mention="@b.jpg", media_type="image/jpeg"),
        ]
    )

    assert message.startswith("Images cannot be attached to retry or continuation prompts.")
    assert "Send the image in a new message" in message
    assert "Images not attached:\n- " in message
    assert "a.png" in message
    assert "b.jpg" in message


def test_format_history_vision_unsupported_message_uses_modal_structure() -> None:
    message = format_history_vision_unsupported_message("Text Only")

    assert message.startswith('The active model profile "Text Only" does not support image input.')
    assert "This session already contains image attachments in its active history." in message
    assert "Switch to a multimodal profile" in message


def test_attachment_error_display_matches_legacy_composer_for_each_plural_form() -> None:
    errors = ["@{odd}.png: failed\nwith more detail", '@"image with spaces.png": unavailable']

    for selected in (errors[:1], errors):
        assert format_message(attachment_error_display(selected)) == format_attachment_error_message(selected)


def test_vision_unsupported_display_matches_named_legacy_composer_for_each_plural_form(tmp_path: Path) -> None:
    images = [
        ImageReference(path=tmp_path / "one image.png", mention='@"one image.png"', media_type="image/png"),
        ImageReference(path=tmp_path / "two {image}.jpg", mention='@"two {image}.jpg"', media_type="image/jpeg"),
    ]

    for selected in (images[:1], images):
        assert format_message(vision_unsupported_display("  Model {Beta}  ", selected)) == (
            format_vision_unsupported_message("  Model {Beta}  ", selected)
        )


def test_vision_unsupported_display_matches_unnamed_legacy_composer_for_each_plural_form(tmp_path: Path) -> None:
    images = [
        ImageReference(path=tmp_path / "one image.png", mention='@"one image.png"', media_type="image/png"),
        ImageReference(path=tmp_path / "two {image}.jpg", mention='@"two {image}.jpg"', media_type="image/jpeg"),
    ]

    for model_name, selected in (("", images[:1]), ("  \t", images)):
        assert format_message(vision_unsupported_display(model_name, selected)) == format_vision_unsupported_message(
            model_name, selected
        )


def test_retry_image_unsupported_display_matches_legacy_composer_for_each_plural_form(tmp_path: Path) -> None:
    images = [
        ImageReference(path=tmp_path / "one image.png", mention='@"one image.png"', media_type="image/png"),
        ImageReference(path=tmp_path / "two {image}.jpg", mention='@"two {image}.jpg"', media_type="image/jpeg"),
    ]

    for selected in (images[:1], images):
        assert format_message(retry_image_unsupported_display(selected)) == format_retry_image_unsupported_message(
            selected
        )


def test_history_vision_displays_match_named_and_unnamed_legacy_composer() -> None:
    for model_name in ("  Model {Beta}  ", "  \t"):
        assert format_message(history_vision_unsupported_display(model_name)) == (
            format_history_vision_unsupported_message(model_name)
        )


def test_discovery_ignores_mentions_inside_conversation_selections(tmp_path: Path) -> None:
    quoted = Quote(text="Use @missing.png as the example input.", source="assistant")
    text = compose_outgoing_text("what does this line do", [quoted])

    assert discover_image_references(text, tmp_path) == []
    discovered = discover_image_mentions(text, tmp_path)
    assert discovered.mentions == []
    # A quoted mention is conversation content, not a file the user asked to
    # attach: a missing path inside it must not refuse the send either.
    assert discovered.errors == []


def test_discovery_still_finds_body_mentions_beside_selections(tmp_path: Path) -> None:
    shot = _write(tmp_path / "shot.png")
    quoted = Quote(text="earlier @missing.png advice", source=None)
    text = compose_outgoing_text(f"compare with @{shot.name}", [quoted])

    references = discover_image_references(text, tmp_path)

    assert [reference.path for reference in references] == [shot]


def test_replace_image_mentions_with_paths_preserves_selections_block(tmp_path: Path) -> None:
    shot = _write(tmp_path / "shot.png")
    quoted = Quote(text="Use @missing.png as the example input.", source="assistant")
    text = compose_outgoing_text(f"see @{shot.name}", [quoted])

    replaced = replace_image_mentions_with_paths(text, tmp_path)

    assert replaced == compose_outgoing_text(f"see {shot}", [quoted])
