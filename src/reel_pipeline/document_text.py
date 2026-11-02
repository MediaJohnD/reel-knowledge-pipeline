"""Turns downloaded bytes into plain text, and decides what kind of file they are.

Used by text_fetcher.py, which owns the fetching. Everything here works on bytes
already in memory and makes no network calls.

Standard library plus pypdf only, by design (no new document dependencies). XML is
read with regular expressions, never an XML parser, so a hostile document can't
trigger entity expansion. Zip members are size-checked before they are read.
"""

from __future__ import annotations

import codecs
import html as html_lib
import io
import json
import posixpath
import re
import struct
import zipfile
import zlib
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse

import pypdf

from reel_pipeline.models import AUDIO_SUFFIXES, IMAGE_SUFFIXES, VIDEO_SUFFIXES

ResponseKind = Literal[
    "html", "doc", "text", "json", "feed", "audio", "video", "image", "hls", "attach"
]

# Zip-bomb guard: a document's members may declare at most this much, in total.
_MAX_UNZIPPED_BYTES = 200 * 1024 * 1024
_MAX_FEED_ITEMS = 200
_MAX_PDF_IMAGES = 40
_MAX_PDF_IMAGE_BYTES = 75_000_000  # pypdf won't decode a stream past this anyway
_MAX_PDF_IMAGE_TOTAL_BYTES = 256 * 1024 * 1024

_ZIP_DOC_SUFFIXES = (".docx", ".pptx", ".xlsx", ".epub", ".odt", ".ods", ".odp")
_DOC_SUFFIXES = (".pdf", *_ZIP_DOC_SUFFIXES)
_LEGACY_OFFICE_SUFFIXES = (".doc", ".ppt", ".xls")
_TEXT_SUFFIXES = (".csv", ".tsv", ".txt", ".md", ".markdown", ".yaml", ".yml", ".log", ".rst")
_FEED_SUFFIXES = (".xml", ".rss", ".atom")

_CONTENT_TYPE_SUFFIXES = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/epub+zip": ".epub",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.presentation": ".odp",
}
_LEGACY_OFFICE_TYPES = (
    "application/msword",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
)
_HLS_TYPES = ("application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl")
_HTML_TYPES = ("text/html", "application/xhtml+xml")
_FEED_TYPES = ("application/rss+xml", "application/atom+xml")
_XML_TYPES = ("text/xml", "application/xml")
_TEXTY_APPLICATION_TYPES = ("application/yaml", "application/x-yaml", "application/csv")

_HTML_START_RE = re.compile(
    rb"\s*(?:<!--.*?-->\s*)*<(?:!doctype\s+html|html|head|body)\b", re.I | re.S
)
_TAG_RE = re.compile(r"<[^>]+>")
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)
_DOCTYPE_RE = re.compile(r"<!DOCTYPE[^>\[]*(?:\[.*?\])?\s*>", re.DOTALL | re.IGNORECASE)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_PROCESSING_RE = re.compile(r"<\?.*?\?>", re.DOTALL)
_NON_CONTENT_RE = re.compile(r"<(script|style|head)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_BLOCK_END_RE = re.compile(r"</(?:p|div|h[1-6]|li|tr|section|article|blockquote)>|<br\s*/?>", re.I)


def looks_like_html(head: bytes) -> bool:
    return bool(_HTML_START_RE.match(head.removeprefix(codecs.BOM_UTF8)[:4096]))


def _decodes_as_text(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    # Incremental, so a multi-byte character cut off at the end of the head is fine.
    try:
        codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
    except UnicodeDecodeError:
        return False
    return True


def url_suffix(url: str) -> str:
    return Path(unquote(urlparse(url).path)).suffix.lower()


def _kind_from_suffix(suffix: str, head: bytes) -> ResponseKind | None:
    if suffix == ".ts":
        # The same suffix is both MPEG transport stream and TypeScript source.
        return "video" if head[:1] == b"\x47" or not _decodes_as_text(head) else "text"
    kinds: list[tuple[tuple[str, ...], ResponseKind]] = [
        ((".m3u8",), "hls"),
        (_DOC_SUFFIXES, "doc"),
        (_LEGACY_OFFICE_SUFFIXES, "attach"),
        ((".svg",), "attach"),
        (_TEXT_SUFFIXES, "text"),
        ((".json",), "json"),
        (_FEED_SUFFIXES, "feed"),
        ((".html", ".htm", ".xhtml"), "html"),
        (VIDEO_SUFFIXES, "video"),
        (AUDIO_SUFFIXES, "audio"),
        (IMAGE_SUFFIXES, "image"),
    ]
    for suffixes, kind in kinds:
        if suffix in suffixes:
            return kind
    return None


def classify_response(head: bytes, content_type: str, url: str, name: str = "") -> ResponseKind:
    """What a response is, from its first bytes, its content type, and its suffix, in
    that order. `name` is the Content-Disposition filename, when there is one.
    """
    ctype = content_type.split(";")[0].strip().lower()
    suffix = Path(name).suffix.lower() or url_suffix(url)

    if head.lstrip(codecs.BOM_UTF8 + b" \t\r\n").startswith(b"#EXTM3U") or ctype in _HLS_TYPES:
        return "hls"
    if head.startswith(b"%PDF-"):
        return "doc"
    if head.startswith(b"PK\x03\x04"):
        known_type = _CONTENT_TYPE_SUFFIXES.get(ctype)
        return "doc" if suffix in _ZIP_DOC_SUFFIXES or known_type else "attach"

    if ctype == "image/svg+xml" or suffix == ".svg":
        return "attach"
    if ctype.startswith("image/"):
        return "image"
    if ctype.startswith("audio/") or ctype == "application/ogg":
        return "audio"
    if ctype.startswith("video/"):
        return "video"
    if ctype in _CONTENT_TYPE_SUFFIXES:
        return "doc"
    if ctype in _LEGACY_OFFICE_TYPES:
        return "attach"
    if ctype in _HTML_TYPES:
        return "html"
    if ctype in _FEED_TYPES or (ctype.endswith("+xml") and ctype not in _HTML_TYPES):
        return "feed"
    if ctype in _XML_TYPES:
        return "html" if looks_like_html(head) else "feed"
    if ctype == "application/json" or ctype.endswith("+json"):
        return "json"
    if ctype.startswith("text/") or ctype in _TEXTY_APPLICATION_TYPES:
        if looks_like_html(head):
            return "html"
        if suffix == ".json":
            return "json"
        if suffix in _FEED_SUFFIXES:
            return "feed"
        return "text"

    from_suffix = _kind_from_suffix(suffix, head)
    if from_suffix is not None:
        return from_suffix
    if looks_like_html(head):
        return "html"
    return "text" if _decodes_as_text(head) else "attach"


def document_suffix(head: bytes, content_type: str) -> str:
    """The right suffix for a document whose name lacks one, or ''."""
    if head.startswith(b"%PDF-"):
        return ".pdf"
    return _CONTENT_TYPE_SUFFIXES.get(content_type.split(";")[0].strip().lower(), "")


def _html_to_text(markup: str) -> str:
    markup = _NON_CONTENT_RE.sub("", markup)
    markup = _BLOCK_END_RE.sub("\n", markup)
    return _tidy(html_lib.unescape(_TAG_RE.sub("", markup)))


def _tidy(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


class _SizeGuardedZip:
    """Reads zip members while keeping a running total of their declared sizes.

    ZipFile stops at a member's declared size, so the declared size is a real limit
    on what a read can produce, not just a claim.
    """

    def __init__(self, archive: zipfile.ZipFile):
        self._archive = archive
        self._total = 0

    def names(self) -> list[str]:
        return self._archive.namelist()

    def read(self, name: str) -> str:
        self._total += self._archive.getinfo(name).file_size
        if self._total > _MAX_UNZIPPED_BYTES:
            raise ValueError(f"document unzips to more than {_MAX_UNZIPPED_BYTES} bytes")
        return self._archive.read(name).decode("utf-8", "replace")


def _numbered(names: list[str], pattern: str) -> list[str]:
    return sorted(
        (n for n in names if re.fullmatch(pattern, n)),
        key=lambda n: int(re.sub(r"\D", "", n) or 0),
    )


def _word_or_slides_text(archive: _SizeGuardedZip) -> str:
    parts = _numbered(archive.names(), r"word/document\.xml|ppt/slides/slide\d+\.xml")
    xml = "\n".join(archive.read(n) for n in parts)
    xml = re.sub(r"</(w:p|a:p)>", "\n", xml)
    return html_lib.unescape(_TAG_RE.sub("", xml)).strip()


_XLSX_SHARED_RE = re.compile(r"<si\b[^>]*>(.*?)</si>", re.DOTALL)
_XLSX_TEXT_RE = re.compile(r"<t\b[^>]*>(.*?)</t>", re.DOTALL)
_XLSX_ROW_RE = re.compile(r"<row\b[^>]*?(?:/>|>(.*?)</row>)", re.DOTALL)
_XLSX_CELL_RE = re.compile(r"<c\b([^>]*?)(?:/>|>(.*?)</c>)", re.DOTALL)
_XLSX_VALUE_RE = re.compile(r"<v\b[^>]*>(.*?)</v>", re.DOTALL)
_XLSX_TYPE_RE = re.compile(r'\bt="(\w+)"')
_XLSX_SHEET_NAME_RE = re.compile(r'<sheet\b[^>]*?\bname="([^"]*)"')


def _xlsx_cell(attributes: str, body: str, shared: list[str]) -> str:
    kind = _XLSX_TYPE_RE.search(attributes)
    if kind and kind[1] == "inlineStr":
        return html_lib.unescape("".join(_XLSX_TEXT_RE.findall(body)))
    value = _XLSX_VALUE_RE.search(body)
    if value is None:
        return ""
    if kind and kind[1] == "s":
        index = int(value[1])
        return shared[index] if 0 <= index < len(shared) else ""
    return html_lib.unescape(value[1])


def _xlsx_text(archive: _SizeGuardedZip) -> str:
    names = archive.names()
    shared: list[str] = []
    if "xl/sharedStrings.xml" in names:
        shared = [
            html_lib.unescape("".join(_XLSX_TEXT_RE.findall(si)))
            for si in _XLSX_SHARED_RE.findall(archive.read("xl/sharedStrings.xml"))
        ]
    sheet_names: list[str] = []
    if "xl/workbook.xml" in names:
        sheet_names = [
            html_lib.unescape(n)
            for n in _XLSX_SHEET_NAME_RE.findall(archive.read("xl/workbook.xml"))
        ]

    sections: list[str] = []
    for position, sheet in enumerate(_numbered(names, r"xl/worksheets/sheet\d+\.xml")):
        rows: list[str] = []
        for row in _XLSX_ROW_RE.findall(archive.read(sheet)):
            cells = [_xlsx_cell(a, b, shared) for a, b in _XLSX_CELL_RE.findall(row)]
            line = "\t".join(cells).rstrip("\t")
            if line:
                rows.append(line)
        title = sheet_names[position] if position < len(sheet_names) else f"Sheet {position + 1}"
        sections.append(f"## {title}\n\n" + "\n".join(rows))
    return "\n\n".join(sections)


_OPF_PATH_RE = re.compile(r'full-path="([^"]+)"')
_OPF_ITEM_RE = re.compile(r"<item\b[^>]*>")
_OPF_ATTR_RE = re.compile(r'\b(id|href)="([^"]*)"')
_OPF_SPINE_RE = re.compile(r'<itemref\b[^>]*?\bidref="([^"]+)"')


def _epub_members(archive: _SizeGuardedZip) -> list[str]:
    names = archive.names()
    fallback = sorted(n for n in names if n.lower().endswith((".xhtml", ".html", ".htm")))
    if "META-INF/container.xml" not in names:
        return fallback
    opf_path = _OPF_PATH_RE.search(archive.read("META-INF/container.xml"))
    if opf_path is None or opf_path[1] not in names:
        return fallback
    opf = archive.read(opf_path[1])
    hrefs: dict[str, str] = {}
    for tag in _OPF_ITEM_RE.findall(opf):
        attributes = dict(_OPF_ATTR_RE.findall(tag))
        if "id" in attributes and "href" in attributes:
            hrefs[attributes["id"]] = attributes["href"]
    base = posixpath.dirname(opf_path[1])
    ordered: list[str] = []
    for idref in _OPF_SPINE_RE.findall(opf):
        if idref in hrefs:
            member = posixpath.normpath(posixpath.join(base, unquote(hrefs[idref])))
            if member in names:
                ordered.append(member)
    return ordered or fallback


def _epub_text(archive: _SizeGuardedZip) -> str:
    chapters = (_html_to_text(archive.read(member)) for member in _epub_members(archive))
    return "\n\n".join(chapter for chapter in chapters if chapter)


def _open_document_text(archive: _SizeGuardedZip) -> str:
    xml = archive.read("content.xml")
    xml = re.sub(r"</text:p>\s*</table:table-cell>", "\t", xml)
    xml = xml.replace("</table:table-row>", "\n")
    xml = re.sub(r"</text:(?:p|h)>", "\n", xml)
    xml = re.sub(r"<text:tab\s*/>", "\t", xml)
    xml = re.sub(r"<text:s\b[^>]*/>", " ", xml)
    text = html_lib.unescape(_TAG_RE.sub("", xml))
    return "\n".join(line.rstrip("\t ") for line in text.splitlines()).strip()


def _pdf_text(data: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(data))
    return "\n\n".join((p.extract_text() or "").strip() for p in reader.pages).strip()


def binary_text(data: bytes, name: str) -> str:
    """Best-effort plain text from a document; '' for anything else, including a
    corrupt, encrypted or oversized one, which still gets attached.
    """
    suffix = Path(name).suffix.lower()
    readers = {
        ".docx": _word_or_slides_text,
        ".pptx": _word_or_slides_text,
        ".xlsx": _xlsx_text,
        ".epub": _epub_text,
        ".odt": _open_document_text,
        ".ods": _open_document_text,
        ".odp": _open_document_text,
    }
    try:
        if suffix == ".pdf":
            return _pdf_text(data)
        reader = readers.get(suffix)
        if reader is None:
            return ""
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return reader(_SizeGuardedZip(archive)).strip()
    except Exception:  # noqa: BLE001 - any failure here just means "no text"
        return ""


def _png(width: int, height: int, components: int, pixels: bytes) -> bytes:
    def _chunk(tag: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(tag + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", crc)

    color_type = 2 if components == 3 else 0
    stride = width * components
    rows = b"".join(
        b"\x00" + pixels[offset : offset + stride] for offset in range(0, height * stride, stride)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(rows))
        + _chunk(b"IEND", b"")
    )


def _color_components(color_space: object) -> int | None:
    resolved = color_space.get_object() if hasattr(color_space, "get_object") else color_space  # type: ignore[union-attr]
    if resolved == "/DeviceRGB":
        return 3
    if resolved == "/DeviceGray":
        return 1
    if isinstance(resolved, list) and resolved and resolved[0] == "/ICCBased":
        components = resolved[1].get_object().get("/N")
        return int(components) if components in (1, 3) else None
    return None


def _xobject_image(xobject: object, stem: str) -> tuple[str, bytes] | None:
    filters = xobject.get("/Filter")  # type: ignore[attr-defined]
    last_filter = filters[-1] if isinstance(filters, list) and filters else filters
    if last_filter == "/DCTDecode":
        return f"{stem}.jpg", xobject.get_data()  # type: ignore[attr-defined]
    if last_filter not in (None, "/FlateDecode", "/LZWDecode"):
        return None  # CCITT, JBIG2, JPX: no decoder without another dependency
    width, height = int(xobject["/Width"]), int(xobject["/Height"])  # type: ignore[index]
    components = _color_components(xobject.get("/ColorSpace"))  # type: ignore[attr-defined]
    if (
        components is None
        or xobject.get("/BitsPerComponent") != 8  # type: ignore[attr-defined]
        or xobject.get("/DecodeParms") is not None  # type: ignore[attr-defined]
        or width * height * components > _MAX_PDF_IMAGE_BYTES
    ):
        return None
    data = xobject.get_data()  # type: ignore[attr-defined]
    if len(data) < width * height * components:
        return None
    return f"{stem}.png", _png(width, height, components, data)


def pdf_images(data: bytes) -> list[tuple[str, bytes]]:
    """(filename, bytes) for each page image of a scanned PDF, in page order.

    Reads the image objects directly rather than through pypdf's page.images, which
    needs Pillow to work, and Pillow isn't (and won't become) a dependency here. JPEG
    streams, which is what most scanners produce, are the file as stored. Bilevel
    CCITT/JBIG2 scans can't be decoded without another library and are left out.
    """
    images: list[tuple[str, bytes]] = []
    total_bytes = 0
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        for page_number, page in enumerate(reader.pages, start=1):
            resources = page.get("/Resources")
            resources = resources.get_object() if resources is not None else {}
            xobjects = resources.get("/XObject")
            xobjects = xobjects.get_object() if xobjects is not None else {}
            index = 0
            for reference in xobjects.values():
                xobject = reference.get_object()
                if xobject.get("/Subtype") != "/Image":
                    continue
                index += 1
                image = _xobject_image(xobject, f"page-{page_number:03d}-{index}")
                if image is not None:
                    total_bytes += len(image[1])
                    if total_bytes > _MAX_PDF_IMAGE_TOTAL_BYTES:
                        return images
                    images.append(image)
                if len(images) >= _MAX_PDF_IMAGES:
                    return images
    except Exception:  # noqa: BLE001 - a corrupt PDF just has no images to hand on
        return images
    return images


_FEED_ITEM_RE = re.compile(r"<(?:\w+:)?(item|entry)\b[^>]*>(.*?)</(?:\w+:)?\1>", re.DOTALL | re.I)
_ATOM_LINK_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_HREF_RE = re.compile(r'\bhref="([^"]*)"')
_REL_RE = re.compile(r'\brel="([^"]*)"')


def _xml_value(raw: str) -> str:
    """One element's text: CDATA unwrapped, markup dropped, entities resolved once.
    Feeds often carry escaped HTML, which only shows as markup after unescaping.
    """
    value = _CDATA_RE.sub(lambda m: m[1], raw)
    value = _TAG_RE.sub(" ", value)
    value = html_lib.unescape(value)
    value = _TAG_RE.sub(" ", value)
    return re.sub(r"[ \t]+", " ", value).strip()


def _feed_field(block: str, *names: str) -> str:
    for name in names:
        match = re.search(
            rf"<(?:\w+:)?{name}\b[^>]*>(.*?)</(?:\w+:)?{name}>", block, re.DOTALL | re.IGNORECASE
        )
        if match:
            value = _xml_value(match[1])
            if value:
                return value
    return ""


def _feed_link(block: str) -> str:
    plain = _feed_field(block, "link")
    if plain:
        return plain
    for tag in _ATOM_LINK_RE.findall(block):
        href = _HREF_RE.search(tag)
        rel = _REL_RE.search(tag)
        if href and (rel is None or rel[1] == "alternate"):
            return html_lib.unescape(href[1])
    return ""


def feed_text(xml: str) -> str:
    """An RSS or Atom feed as markdown, or '' if it has no <item>/<entry>."""
    items = list(_FEED_ITEM_RE.finditer(xml))
    if not items:
        return ""
    title = _feed_field(xml[: items[0].start()], "title")
    sections = [f"# {title}"] if title else []
    for item in items[:_MAX_FEED_ITEMS]:
        block = item[2]
        lines = [f"## {_feed_field(block, 'title') or '(untitled)'}"]
        link = _feed_link(block)
        date = _feed_field(block, "pubDate", "published", "updated", "date")
        body = _feed_field(block, "encoded", "content", "description", "summary")
        lines.extend(part for part in (date, link, body) if part)
        sections.append("\n".join(lines))
    if len(items) > _MAX_FEED_ITEMS:
        sections.append(f"(Only the first {_MAX_FEED_ITEMS} of {len(items)} items were kept.)")
    return "\n\n".join(sections)


def strip_xml_tags(xml: str) -> str:
    """Plain text from XML that isn't a feed, without parsing it."""
    text = _DOCTYPE_RE.sub("", xml)
    text = _COMMENT_RE.sub("", text)
    text = _PROCESSING_RE.sub("", text)
    text = _CDATA_RE.sub(lambda m: html_lib.escape(m[1]), text)
    text = _TAG_RE.sub("\n", text)
    return _tidy(html_lib.unescape(text))


def pretty_json(text: str) -> str:
    """Indented JSON, or the text unchanged if it doesn't parse."""
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except (ValueError, RecursionError):
        return text
