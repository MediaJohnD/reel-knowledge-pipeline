from __future__ import annotations

import io
import json
import struct
import zipfile
import zlib

import pypdf
import pypdf.filters
import pytest
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from reel_pipeline import document_text
from reel_pipeline.document_text import (
    binary_text,
    classify_response,
    feed_text,
    pdf_images,
    pretty_json,
    strip_xml_tags,
)


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    return buf.getvalue()


def pdf_with_text(text: str) -> bytes:
    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(300, 300)
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 10 100 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def scanned_pdf(image: bytes, *, filter_name: str = "/DCTDecode", color="/DeviceRGB") -> bytes:
    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(300, 300)
    stream = DecodedStreamObject()
    stream.set_data(image)
    stream.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(1),
            NameObject("/Height"): NumberObject(1),
            NameObject("/BitsPerComponent"): NumberObject(8),
            NameObject("/ColorSpace"): NameObject(color),
        }
    )
    if filter_name:
        stream[NameObject("/Filter")] = NameObject(filter_name)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): writer._add_object(stream)})}
    )
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# binary_text


def test_pdf_text():
    assert "Hello scanned world" in binary_text(pdf_with_text("Hello scanned world"), "a.pdf")


def test_xlsx_reads_shared_inline_and_numeric_cells():
    data = _zip(
        {
            "xl/workbook.xml": '<workbook><sheets><sheet name="Prices"/></sheets></workbook>',
            "xl/sharedStrings.xml": (
                "<sst><si><t>Item</t></si><si><r><t>Fan</t></r><r><t>cy</t></r></si>"
                "<si><t>A &amp; B</t></si></sst>"
            ),
            "xl/worksheets/sheet1.xml": (
                "<worksheet><sheetData>"
                '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
                '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>42</v></c></row>'
                '<row r="3"/>'
                '<row r="4"><c r="A4" t="inlineStr"><is><t>inline</t></is></c><c r="B4"/></row>'
                "</sheetData></worksheet>"
            ),
        }
    )
    text = binary_text(data, "book.xlsx")
    assert "Prices" in text
    assert "Item\tFancy" in text
    assert "A & B\t42" in text
    assert "inline" in text


def test_epub_follows_the_spine_order():
    data = _zip(
        {
            "META-INF/container.xml": (
                '<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>'
            ),
            "OPS/book.opf": (
                '<package><manifest><item id="b" href="ch%202.xhtml"/>'
                '<item id="a" href="ch1.xhtml"/></manifest>'
                '<spine><itemref idref="a"/><itemref idref="b"/></spine></package>'
            ),
            "OPS/ch1.xhtml": (
                "<html><head><title>no</title></head><body><p>First &amp; one</p></body></html>"
            ),
            "OPS/ch%202.xhtml": "<html><body><p>wrong member</p></body></html>",
            "OPS/ch 2.xhtml": "<html><body><p>Second chapter</p></body></html>",
        }
    )
    text = binary_text(data, "b.epub")
    assert text.index("First & one") < text.index("Second chapter")
    assert "no" not in text.split()


def test_epub_without_container_falls_back_to_sorted_html():
    data = _zip({"b.html": "<p>two</p>", "a.html": "<p>one</p>"})
    text = binary_text(data, "b.epub")
    assert text.index("one") < text.index("two")


@pytest.mark.parametrize("suffix", [".odt", ".ods", ".odp"])
def test_open_document_reads_content_xml(suffix):
    data = _zip(
        {
            "content.xml": (
                "<office:document-content><office:body><text:p>Hello &lt;world&gt;</text:p>"
                "<table:table-row><table:table-cell><text:p>a</text:p></table:table-cell>"
                "<table:table-cell><text:p>b</text:p></table:table-cell></table:table-row>"
                "</office:body></office:document-content>"
            )
        }
    )
    text = binary_text(data, f"f{suffix}")
    assert "Hello <world>" in text
    assert "a\tb" in text


def test_docx_still_works():
    data = _zip({"word/document.xml": "<w:p><w:t>Doc body</w:t></w:p><w:p><w:t>Two</w:t></w:p>"})
    assert binary_text(data, "x.docx") == "Doc body\nTwo"


def test_zip_bomb_guard_refuses_by_declared_size(monkeypatch):
    monkeypatch.setattr(document_text, "_MAX_UNZIPPED_BYTES", 10)
    data = _zip({"word/document.xml": "<w:p>" + "x" * 100 + "</w:p>"})
    assert binary_text(data, "x.docx") == ""


def test_zip_bomb_guard_counts_all_members_together(monkeypatch):
    monkeypatch.setattr(document_text, "_MAX_UNZIPPED_BYTES", 150)
    data = _zip(
        {
            "ppt/slides/slide1.xml": "x" * 100,
            "ppt/slides/slide2.xml": "y" * 100,
        }
    )
    assert binary_text(data, "x.pptx") == ""


def test_corrupt_or_unknown_binary_is_empty():
    assert binary_text(b"not a zip", "x.xlsx") == ""
    assert binary_text(b"whatever", "x.bin") == ""


# Feeds


RSS = """<?xml version="1.0"?>
<!DOCTYPE rss [<!ENTITY boom "BOOM">]>
<rss version="2.0"><channel><title>My Feed</title>
<item><title>First &amp; best</title><link>https://example.com/1</link>
<pubDate>Sat, 01 Nov 2026 10:00:00 GMT</pubDate>
<description><![CDATA[<p>Hello <b>there</b> &boom;</p>]]></description></item>
<item><title>Second</title><link>https://example.com/2</link></item>
</channel></rss>"""

ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Feed</title>
<entry><title>One</title><link href="https://example.com/a"/>
<updated>2026-11-01T00:00:00Z</updated><summary>Sum text</summary></entry></feed>"""


def test_rss_feed():
    text = feed_text(RSS)
    assert text.startswith("# My Feed")
    assert "## First & best" in text
    assert "https://example.com/1" in text
    assert "Hello there" in text
    assert "01 Nov 2026" in text
    assert "## Second" in text
    # No entity expansion: the custom DTD entity stays inert.
    assert "BOOM" not in text


def test_atom_feed():
    text = feed_text(ATOM)
    assert text.startswith("# Atom Feed")
    assert "## One" in text
    assert "https://example.com/a" in text
    assert "Sum text" in text


def test_feed_caps_item_count():
    body = "".join(f"<item><title>t{i}</title></item>" for i in range(205))
    text = feed_text(f"<rss><channel><title>x</title>{body}</channel></rss>")
    assert "## t199" in text
    assert "## t200" not in text
    assert "200 of 205" in text


def test_feed_without_items_is_empty_and_xml_tags_strip():
    xml = "<?xml version='1.0'?><root><a>Hello</a><b>A &amp; B</b><![CDATA[raw]]></root>"
    assert feed_text(xml) == ""
    stripped = strip_xml_tags(xml)
    assert "Hello" in stripped and "A & B" in stripped and "raw" in stripped
    assert "<" not in stripped


# JSON


def test_pretty_json():
    assert pretty_json('{"a":[1,2]}') == json.dumps({"a": [1, 2]}, indent=2, ensure_ascii=False)
    assert pretty_json("not json") == "not json"


# PDF images


def test_pdf_images_returns_raw_jpeg_without_pillow():
    jpeg = b"\xff\xd8\xff\xe0fakejpeg\xff\xd9"
    images = pdf_images(scanned_pdf(jpeg))
    assert images == [("page-001-1.jpg", jpeg)]


def test_pdf_images_turns_flate_pixels_into_a_png():
    images = pdf_images(scanned_pdf(bytes([1, 2, 3]), filter_name=""))
    name, data = images[0]
    assert name == "page-001-1.png"
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    ihdr = data[16:24]
    assert struct.unpack(">II", ihdr) == (1, 1)
    idat_len = struct.unpack(">I", data[33:37])[0]
    assert zlib.decompress(data[41 : 41 + idat_len]) == bytes([0, 1, 2, 3])


def test_pdf_images_on_a_corrupt_pdf_is_empty():
    assert pdf_images(b"%PDF-1.4 garbage") == []


class _UndecodedImage(dict):
    def get_data(self):
        pytest.fail("this image should be turned down before its stream is decoded")


@pytest.mark.parametrize(
    "fields",
    [
        {"/Filter": "/CCITTFaxDecode"},
        {
            "/Filter": "/FlateDecode",
            "/Width": 100_000,
            "/Height": 100_000,
            "/ColorSpace": "/DeviceRGB",
            "/BitsPerComponent": 8,
        },
    ],
)
def test_pdf_image_that_cannot_be_used_is_not_decoded(fields):
    assert document_text._xobject_image(_UndecodedImage(fields), "page") is None


def test_pdf_images_stop_at_the_total_size_limit(monkeypatch):
    jpeg = b"\xff\xd8\xff\xe0fakejpeg\xff\xd9"
    monkeypatch.setattr(document_text, "_MAX_PDF_IMAGE_TOTAL_BYTES", len(jpeg) - 1)
    assert pdf_images(scanned_pdf(jpeg)) == []


def test_pypdf_still_caps_how_far_one_stream_can_expand():
    # What keeps a small compressed stream from becoming a huge allocation in get_data().
    assert pypdf.filters.ZLIB_MAX_OUTPUT_LENGTH <= 75_000_000
    assert pypdf.filters.LZW_MAX_OUTPUT_LENGTH <= 75_000_000


# Classification


@pytest.mark.parametrize(
    ("head", "content_type", "url", "expected"),
    [
        (b"%PDF-1.7 ...", "text/html", "https://e.com/x", "doc"),
        (b"PK\x03\x04abc", "application/octet-stream", "https://e.com/x.docx", "doc"),
        (b"PK\x03\x04abc", "application/octet-stream", "https://e.com/x.zip", "attach"),
        (
            b"PK\x03\x04abc",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "https://e.com/download",
            "doc",
        ),
        (b"PK\x03\x04abc", "application/epub+zip", "https://e.com/download", "doc"),
        (b"#EXTM3U\n", "text/plain", "https://e.com/x", "hls"),
        (b"", "application/vnd.apple.mpegurl", "https://e.com/x", "hls"),
        (b"<html><body>hi</body></html>", "text/html; charset=utf-8", "https://e.com/x", "html"),
        (b"<html><body>hi</body></html>", "text/plain", "https://e.com/x", "html"),
        (b"plain words", "text/plain", "https://e.com/x", "text"),
        (b"a,b\n1,2\n", "text/csv", "https://e.com/x", "text"),
        (b"# h\n", "text/markdown", "https://e.com/x", "text"),
        (b"a: 1\n", "application/yaml", "https://e.com/x", "text"),
        (b"{}", "application/json", "https://e.com/x", "json"),
        (b"{}", "application/ld+json", "https://e.com/x", "json"),
        (b"{}", "text/plain", "https://e.com/x.json", "json"),
        (b"<rss/>", "application/rss+xml", "https://e.com/x", "feed"),
        (b"<feed/>", "application/atom+xml", "https://e.com/x", "feed"),
        (b"<x/>", "text/xml", "https://e.com/x", "feed"),
        (b"<x/>", "text/plain", "https://e.com/x.xml", "feed"),
        (b"\xff\xfb", "audio/mpeg", "https://e.com/x", "audio"),
        (b"\x00", "video/mp4", "https://e.com/x", "video"),
        (b"\x00", "image/png", "https://e.com/x", "image"),
        (b"<svg/>", "image/svg+xml", "https://e.com/x", "attach"),
        (b"<svg/>", "", "https://e.com/x.svg", "attach"),
        (b"\x00\x01", "application/octet-stream", "https://e.com/x.mp4", "video"),
        (b"\x00\x01", "", "https://e.com/x.flac", "audio"),
        (b"\x00\x01", "", "https://e.com/x.heic", "image"),
        (b"\x00\x01", "", "https://e.com/x.pdf", "doc"),
        (b"a,b\n", "application/octet-stream", "https://e.com/x.tsv", "text"),
        (b"<html>", "application/octet-stream", "https://e.com/x", "html"),
        (b"just text", "", "https://e.com/x", "text"),
        (b"\xff\xfe\x00\x81\x82", "application/octet-stream", "https://e.com/x", "attach"),
        (b"\xd0\xcf\x11\xe0", "application/msword", "https://e.com/x.doc", "attach"),
        (b"\xd0\xcf\x11\xe0", "application/octet-stream", "https://e.com/x.xls", "attach"),
    ],
)
def test_classify_response(head, content_type, url, expected):
    assert classify_response(head, content_type, url) == expected


def test_classify_uses_the_filename_when_the_url_has_no_suffix():
    assert classify_response(b"\x00\x81", "", "https://e.com/dl", name="clip.mp4") == "video"


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"\x47" + b"\x00" * 187 + b"\x47" + b"\x81" * 50, "video"),  # MPEG-TS sync byte
        (b"export const x = 1;\n", "text"),  # TypeScript source
    ],
)
def test_dot_ts_under_octet_stream(head, expected):
    assert classify_response(head, "application/octet-stream", "https://e.com/a.ts") == expected
