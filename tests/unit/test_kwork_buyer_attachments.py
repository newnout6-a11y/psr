from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import zipfile

from src.platforms.kwork_buyer import (
    BuyerAttachmentParseLimits,
    BuyerAttachmentParseStatus,
    BuyerAttachmentType,
    build_attachment_context,
    build_attachment_metadata,
    detect_attachment_type,
    normalize_attachment_filename,
    parse_attachment,
)


def test_filename_normalizer_removes_paths_controls_and_windows_reserved_names() -> None:
    assert normalize_attachment_filename("..\\uploads/  report\x00 .txt ") == "report.txt"
    assert normalize_attachment_filename("CON.txt") == "CON_.txt"


def test_metadata_uses_checksum_and_allowlisted_type_detection() -> None:
    metadata = build_attachment_metadata(
        b"hello",
        filename="brief.txt",
        content_type="text/plain; charset=utf-8",
    )

    assert metadata.attachment_type is BuyerAttachmentType.TEXT
    assert metadata.sha256 == sha256(b"hello").hexdigest()
    assert detect_attachment_type("brief.txt", "image/png") is None
    assert detect_attachment_type("brief.unknown", "application/octet-stream") is None


def test_text_parser_bounds_output_and_redacts_credentials() -> None:
    result = parse_attachment(
        b"Cookie: session=top-secret\nBearer abc.def\n0123456789",
        filename="brief.txt",
        limits=BuyerAttachmentParseLimits(max_characters=30),
    )

    assert result.status is BuyerAttachmentParseStatus.PARSED
    assert "top-secret" not in result.text
    assert "abc.def" not in result.text
    assert any(item.reason == "character_limit" for item in result.omissions)
    assert result.to_manifest()["extracted_characters"] == len(result.text)


def test_json_and_csv_parsers_produce_safe_bounded_text() -> None:
    json_result = parse_attachment(
        b'{"title":"Bot","accessToken":"secret-value","url":"https://example.test/?token=also-secret"}',
        filename="project.json",
    )
    csv_result = parse_attachment(
        b"title,token\nTelegram bot,very-secret\n",
        filename="project.csv",
    )

    assert json_result.status is BuyerAttachmentParseStatus.PARSED
    assert "secret-value" not in json_result.text
    assert "also-secret" not in json_result.text
    assert csv_result.status is BuyerAttachmentParseStatus.PARSED
    assert "very-secret" not in csv_result.text


def test_parser_fails_closed_for_unknown_oversized_and_malformed_content() -> None:
    unsupported = parse_attachment(b"binary", filename="payload.exe", content_type="application/octet-stream")
    oversized = parse_attachment(
        b"12345",
        filename="brief.txt",
        limits=BuyerAttachmentParseLimits(max_bytes=4),
    )
    malformed = parse_attachment(b"{not-json", filename="brief.json")

    assert unsupported.status is BuyerAttachmentParseStatus.UNSUPPORTED
    assert oversized.status is BuyerAttachmentParseStatus.TOO_LARGE
    assert malformed.status is BuyerAttachmentParseStatus.CORRUPT


def test_optional_office_parser_fails_gracefully_when_input_is_not_a_document() -> None:
    result = parse_attachment(b"not a docx", filename="brief.docx")

    assert result.status in {BuyerAttachmentParseStatus.CORRUPT, BuyerAttachmentParseStatus.UNSUPPORTED}
    assert result.text == ""


def test_xml_image_and_zip_parsers_are_bounded_and_honest() -> None:
    xml = parse_attachment(b"<brief><title>Telegram bot</title><token>secret</token></brief>", filename="brief.xml")

    from PIL import Image

    image_stream = BytesIO()
    Image.new("RGB", (8, 6), color="red").save(image_stream, format="PNG")
    image = parse_attachment(image_stream.getvalue(), filename="brief.png")

    archive_stream = BytesIO()
    with zipfile.ZipFile(archive_stream, "w") as archive:
        archive.writestr("scope.txt", "Automation scope")
        archive.writestr("nested.zip", b"not parsed")
    archive = parse_attachment(archive_stream.getvalue(), filename="brief.zip")

    assert xml.status is BuyerAttachmentParseStatus.PARSED
    assert "Telegram bot" in xml.text
    assert "secret" not in xml.text
    assert image.metadata.attachment_type is BuyerAttachmentType.IMAGE
    assert image.status is BuyerAttachmentParseStatus.PARSED
    assert "8x6" in image.text
    assert any(item.reason == "vision_not_configured" for item in image.omissions)
    assert archive.status is BuyerAttachmentParseStatus.PARSED
    assert "Automation scope" in archive.text
    assert any(item.reason == "unsupported_archive_member" for item in archive.omissions)


def test_context_builder_keeps_only_parsed_redacted_bounded_text_and_manifest() -> None:
    first = parse_attachment(b"title: Telegram bot\ntoken=secret", filename="token=filename-secret.txt")
    second = parse_attachment(b"second attachment text", filename="second.txt")
    context = build_attachment_context((first, second), max_total_characters=48)

    assert "secret" not in context.text
    assert "filename-secret" not in str(context.manifest)
    assert len(context.text) <= 48
    assert len(context.context_hash) == 64
    assert len(context.manifest) == 2
    assert any(entry.get("context_omission") == "context_character_limit" for entry in context.manifest)
