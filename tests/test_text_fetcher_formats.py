"""More formats and links, and the fallbacks tried before a URL ends as an error.
See docs/superpowers/specs/2026-11-01-more-formats-and-fallbacks-design.md.
"""

from __future__ import annotations

import gzip
import io
import json
import zipfile
from pathlib import Path

import httpx
import pypdf
import pytest
import respx
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from reel_pipeline.config import (
    DownloadConfig,
    RenderFallbackConfig,
    Settings,
    TextCaptureConfig,
)
from reel_pipeline.models import MediaType
from reel_pipeline.text_fetcher import (
    ArchiveEligibleError,
    DispatchingTextFetcher,
    DriveFetcher,
    GenericHtmlFetcher,
    MediaFound,
    NotionFetcher,
    TextFetchError,
)

_JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-bytes"
_LONG_TEXT = "A real paragraph of plain article text that is long enough. " * 30


def _settings(tmp_path, *, render=False, allowed=("youtube.com",), **capture):
    return Settings(
        project_root=tmp_path,
        download=DownloadConfig(allowed_domains=list(allowed)),
        text_capture=TextCaptureConfig(
            render_fallback=RenderFallbackConfig(enabled=render), **capture
        ),
    )


def _fetch(settings, url, cid="cid-1"):
    return GenericHtmlFetcher(settings).fetch(url, cid)


def _html_page(body="", head=""):
    return f"<!doctype html><html><head>{head}</head><body>{body}</body></html>"


def _html_response(markup, status=200):
    return httpx.Response(status, text=markup, headers={"content-type": "text/html"})


def _pdf(text=None, image=None):
    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(300, 300)
    resources = DictionaryObject()
    if text:
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
        resources[NameObject("/Font")] = DictionaryObject({NameObject("/F1"): font})
    if image:
        stream = DecodedStreamObject()
        stream.set_data(image)
        stream.update(
            {
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Image"),
                NameObject("/Width"): NumberObject(1),
                NameObject("/Height"): NumberObject(1),
                NameObject("/BitsPerComponent"): NumberObject(8),
                NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                NameObject("/Filter"): NameObject("/DCTDecode"),
            }
        )
        resources[NameObject("/XObject")] = DictionaryObject(
            {NameObject("/Im0"): writer._add_object(stream)}
        )
    page[NameObject("/Resources")] = resources
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _wayback_answer(timestamp="20240102030405", status="200", available=True):
    return httpx.Response(
        200,
        json={
            "archived_snapshots": {
                "closest": {
                    "available": available,
                    "status": status,
                    "timestamp": timestamp,
                    "url": f"http://web.archive.org/web/{timestamp}/x",
                }
            }
        },
    )


# --- text-shaped formats ----------------------------------------------------


@respx.mock
def test_csv_is_captured_as_text(tmp_path):
    respx.get("https://example.com/data.csv").mock(
        return_value=httpx.Response(200, text="a,b\n1,2\n", headers={"content-type": "text/csv"})
    )
    result = _fetch(_settings(tmp_path), "https://example.com/data.csv")
    assert result.text == "a,b\n1,2"
    assert result.content_kind == "text"
    assert result.backend == "text-fetch"


@respx.mock
def test_json_is_pretty_printed(tmp_path):
    respx.get("https://example.com/d.json").mock(
        return_value=httpx.Response(
            200, text='{"a":[1,2]}', headers={"content-type": "application/json"}
        )
    )
    result = _fetch(_settings(tmp_path), "https://example.com/d.json")
    assert result.text == json.dumps({"a": [1, 2]}, indent=2)


@respx.mock
def test_rss_feed_becomes_markdown(tmp_path):
    feed = (
        "<rss><channel><title>My Feed</title><item><title>First</title>"
        "<link>https://example.com/1</link><description>Hello</description></item></channel></rss>"
    )
    respx.get("https://example.com/feed").mock(
        return_value=httpx.Response(200, text=feed, headers={"content-type": "application/rss+xml"})
    )
    result = _fetch(_settings(tmp_path), "https://example.com/feed")
    assert result.text.startswith("# My Feed")
    assert "## First" in result.text
    assert "https://example.com/1" in result.text


@respx.mock
def test_xml_without_items_has_its_tags_stripped(tmp_path):
    respx.get("https://example.com/a.xml").mock(
        return_value=httpx.Response(
            200, text="<root><a>one</a><b>two</b></root>", headers={"content-type": "text/xml"}
        )
    )
    result = _fetch(_settings(tmp_path), "https://example.com/a.xml")
    assert "one" in result.text
    assert "<" not in result.text


@respx.mock
def test_empty_text_response_is_an_error(tmp_path):
    respx.get("https://example.com/empty.txt").mock(
        return_value=httpx.Response(200, text="  \n", headers={"content-type": "text/plain"})
    )
    with pytest.raises(TextFetchError, match="no usable text"):
        _fetch(_settings(tmp_path), "https://example.com/empty.txt")


# --- documents and attachments ----------------------------------------------


@respx.mock
def test_pdf_with_text_is_attached_and_extracted(tmp_path):
    settings = _settings(tmp_path)
    data = _pdf(text="Hello from a PDF")
    respx.get("https://example.com/report.pdf").mock(
        return_value=httpx.Response(200, content=data, headers={"content-type": "application/pdf"})
    )
    result = _fetch(settings, "https://example.com/report.pdf")
    assert (settings.vault_dir / "attachments" / "report.pdf").read_bytes() == data
    assert result.text.startswith("![[report.pdf]]")
    assert "Hello from a PDF" in result.text
    assert result.backend == "file-attachment"


@respx.mock
def test_pdf_name_without_suffix_gets_one_from_magic_bytes(tmp_path):
    settings = _settings(tmp_path)
    respx.get("https://example.com/download").mock(
        return_value=httpx.Response(
            200, content=_pdf(text="Named by magic"), headers={"content-type": "application/pdf"}
        )
    )
    result = _fetch(settings, "https://example.com/download")
    assert (settings.vault_dir / "attachments" / "download.pdf").exists()
    assert "Named by magic" in result.text


@pytest.mark.parametrize(
    ("disposition", "expected"),
    [
        ('attachment; filename="plan.docx"', "plan.docx"),
        ("attachment; filename*=UTF-8''my%20plan.docx", "my plan.docx"),
        ('attachment; filename="../../evil.docx"', "evil.docx"),
        ('attachment; filename="a[b]#c^.docx"', "abc.docx"),
        ('attachment; filename="CON.docx"', "file-CON.docx"),
    ],
)
@respx.mock
def test_attachment_names_are_sanitized(tmp_path, disposition, expected):
    settings = _settings(tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:p><w:t>Body</w:t></w:p>")
    respx.get("https://example.com/get").mock(
        return_value=httpx.Response(
            200,
            content=buf.getvalue(),
            headers={
                "content-type": "application/octet-stream",
                "content-disposition": disposition,
            },
        )
    )
    result = _fetch(settings, "https://example.com/get")
    assert (settings.vault_dir / "attachments" / expected).exists()
    assert result.text.startswith(f"![[{expected}]]")
    # Nothing escaped the attachments folder.
    assert not (settings.vault_dir / "evil.docx").exists()


@respx.mock
def test_svg_attaches_without_text(tmp_path):
    settings = _settings(tmp_path)
    respx.get("https://example.com/logo.svg").mock(
        return_value=httpx.Response(
            200, text="<svg></svg>", headers={"content-type": "image/svg+xml"}
        )
    )
    result = _fetch(settings, "https://example.com/logo.svg")
    assert (settings.vault_dir / "attachments" / "logo.svg").exists()
    assert "no text extracted" in result.text


@respx.mock
def test_scanned_pdf_hands_its_page_images_on_as_media(tmp_path):
    settings = _settings(tmp_path)
    respx.get("https://example.com/scan.pdf").mock(
        return_value=httpx.Response(
            200, content=_pdf(image=_JPEG), headers={"content-type": "application/pdf"}
        )
    )
    with pytest.raises(MediaFound) as caught:
        _fetch(settings, "https://example.com/scan.pdf", "cid-scan")
    found = caught.value
    assert found.url is None
    assert found.result is not None
    assert found.result.media_type is MediaType.IMAGE
    assert "![[scan.pdf]]" in found.context
    paths = [p.replace("\\", "/") for p in found.result.media_paths]
    assert paths and all("/cid-scan/" in p for p in paths)
    with open(found.result.media_paths[0], "rb") as handle:
        assert handle.read() == _JPEG


@respx.mock
def test_document_over_the_cap_is_a_clear_error_and_leaves_nothing(tmp_path):
    settings = _settings(tmp_path, max_document_mb=1)
    big = b"%PDF-" + b"x" * (1024 * 1024 + 10)
    respx.get("https://example.com/big.pdf").mock(
        return_value=httpx.Response(200, content=big, headers={"content-type": "application/pdf"})
    )
    with pytest.raises(TextFetchError, match="1 MB"):
        _fetch(settings, "https://example.com/big.pdf")
    assert not (settings.vault_dir / "attachments" / "big.pdf").exists()


# --- direct media -----------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "ctype", "name", "media_type"),
    [
        ("https://example.com/clip.mp4", "video/mp4", "file.mp4", MediaType.VIDEO),
        ("https://example.com/talk.mp3", "audio/mpeg", "file.mp3", MediaType.VIDEO),
        ("https://example.com/talk", "audio/flac", "file.flac", MediaType.VIDEO),
        ("https://example.com/pic.png", "image/png", "file.png", MediaType.IMAGE),
        ("https://example.com/pic.heic", "application/octet-stream", "file.heic", MediaType.IMAGE),
    ],
)
@respx.mock
def test_direct_media_streams_to_tmp_and_is_handed_on(tmp_path, url, ctype, name, media_type):
    settings = _settings(tmp_path)
    respx.get(url).mock(
        return_value=httpx.Response(200, content=b"\x00\x01media", headers={"content-type": ctype})
    )
    with pytest.raises(MediaFound) as caught:
        _fetch(settings, url, "cid-m")
    result = caught.value.result
    assert result is not None
    assert result.media_type is media_type
    assert result.platform == "direct"
    assert caught.value.url is None
    target = settings.tmp_dir / "cid-m" / name
    assert result.media_paths == [str(target)]
    assert target.read_bytes() == b"\x00\x01media"


@respx.mock
def test_media_over_the_cap_is_a_clear_error_and_leaves_no_partial_file(tmp_path):
    settings = _settings(tmp_path, max_media_mb=1)
    respx.get("https://example.com/huge.mp4").mock(
        return_value=httpx.Response(
            200, content=b"\x00" * (1024 * 1024 + 1), headers={"content-type": "video/mp4"}
        )
    )
    with pytest.raises(TextFetchError, match="1 MB"):
        _fetch(settings, "https://example.com/huge.mp4", "cid-big")
    assert not list((settings.tmp_dir / "cid-big").glob("*"))


# --- Google exports ---------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "export"),
    [
        (
            "https://docs.google.com/spreadsheets/d/abc123/edit?gid=77",
            "https://docs.google.com/spreadsheets/d/abc123/export?format=csv&gid=77",
        ),
        (
            "https://docs.google.com/spreadsheets/d/abc123/edit",
            "https://docs.google.com/spreadsheets/d/abc123/export?format=csv",
        ),
        (
            "https://docs.google.com/spreadsheets/u/1/d/abc123/edit",
            "https://docs.google.com/spreadsheets/d/abc123/export?format=csv",
        ),
    ],
)
@respx.mock
def test_google_sheet_uses_csv_export(tmp_path, url, export):
    respx.get(export).mock(
        return_value=httpx.Response(
            200, text="a,b\n1,2\n", headers={"content-type": "text/csv; charset=utf-8"}
        )
    )
    result = _fetch(_settings(tmp_path), url)
    assert result.text == "a,b\n1,2"
    assert result.backend == "gdocs-export"


@respx.mock
def test_google_doc_with_account_prefix_uses_text_export(tmp_path):
    respx.get("https://docs.google.com/document/d/doc1/export?format=txt").mock(
        return_value=httpx.Response(200, text="Body", headers={"content-type": "text/plain"})
    )
    result = _fetch(_settings(tmp_path), "https://docs.google.com/document/u/0/d/doc1/edit")
    assert result.text == "Body"


@respx.mock
def test_private_google_sheet_is_a_clear_error(tmp_path):
    respx.get("https://docs.google.com/spreadsheets/d/abc123/export?format=csv").mock(
        return_value=_html_response(_html_page("Sign in"))
    )
    with pytest.raises(TextFetchError, match="isn't public"):
        _fetch(_settings(tmp_path), "https://docs.google.com/spreadsheets/d/abc123/edit")


@respx.mock
def test_google_export_failure_does_not_go_to_the_archive(tmp_path):
    respx.get("https://docs.google.com/document/d/doc1/export?format=txt").mock(
        return_value=httpx.Response(404)
    )
    wayback = respx.get(url__startswith="https://archive.org/wayback/available")
    with pytest.raises(TextFetchError, match="404") as caught:
        _fetch(_settings(tmp_path), "https://docs.google.com/document/d/doc1/edit")
    assert not wayback.called
    assert not isinstance(caught.value, ArchiveEligibleError)


# --- redirects and embeds ---------------------------------------------------


@respx.mock
def test_redirect_onto_an_allow_listed_media_host_is_handed_to_the_downloader(tmp_path):
    respx.get("https://short.example.com/x").mock(
        return_value=httpx.Response(302, headers={"location": "https://www.youtube.com/watch?v=1"})
    )
    respx.get("https://www.youtube.com/watch?v=1").mock(
        return_value=_html_response(_html_page(_LONG_TEXT))
    )
    with pytest.raises(MediaFound) as caught:
        _fetch(_settings(tmp_path), "https://short.example.com/x")
    assert caught.value.url == "https://www.youtube.com/watch?v=1"
    assert caught.value.result is None


@pytest.mark.parametrize("path", ["/", "/login", "/accounts/login/?next=x", "/signin"])
@respx.mock
def test_redirect_to_a_media_hosts_root_or_login_is_not_media(tmp_path, path):
    respx.get("https://short.example.com/x").mock(
        return_value=httpx.Response(302, headers={"location": f"https://www.youtube.com{path}"})
    )
    respx.get(f"https://www.youtube.com{path}").mock(
        return_value=_html_response(_html_page(f"<p>{_LONG_TEXT}</p>"))
    )
    result = _fetch(_settings(tmp_path), "https://short.example.com/x")
    assert "real paragraph" in result.text


@respx.mock
def test_empty_allow_list_never_counts_a_redirect_as_media(tmp_path):
    respx.get("https://short.example.com/x").mock(
        return_value=httpx.Response(302, headers={"location": "https://other.example.com/watch"})
    )
    respx.get("https://other.example.com/watch").mock(
        return_value=_html_response(_html_page(f"<p>{_LONG_TEXT}</p>"))
    )
    result = _fetch(_settings(tmp_path, allowed=()), "https://short.example.com/x")
    assert "real paragraph" in result.text


@pytest.mark.parametrize(
    "markup",
    [
        '<meta property="og:video" content="https://cdn.example.com/a.mp4">',
        '<meta content="https://cdn.example.com/a.mp4" property="og:video:secure_url">',
        '<meta property="og:video:url" content="https://cdn.example.com/a.mp4">',
        '<meta name="twitter:player:stream" content="https://cdn.example.com/a.mp4">',
        '<video src="https://cdn.example.com/a.mp4"></video>',
        '<video controls><source src="https://cdn.example.com/a.mp4"></video>',
        '<audio src="https://cdn.example.com/a.mp4"></audio>',
    ],
)
@respx.mock
def test_thin_page_with_a_direct_file_downloads_it(tmp_path, markup):
    settings = _settings(tmp_path)
    respx.get("https://example.com/watch").mock(return_value=_html_response(_html_page(markup)))
    respx.get("https://cdn.example.com/a.mp4").mock(
        return_value=httpx.Response(
            200, content=b"\x00\x00vid", headers={"content-type": "video/mp4"}
        )
    )
    with pytest.raises(MediaFound) as caught:
        _fetch(settings, "https://example.com/watch", "cid-e")
    result = caught.value.result
    assert result is not None
    assert (settings.tmp_dir / "cid-e" / "file.mp4").read_bytes() == b"\x00\x00vid"


@respx.mock
def test_relative_embed_is_resolved_against_the_page(tmp_path):
    respx.get("https://example.com/a/watch").mock(
        return_value=_html_response(_html_page('<video src="../v/a.mp4"></video>'))
    )
    route = respx.get("https://example.com/v/a.mp4").mock(
        return_value=httpx.Response(200, content=b"\x00v", headers={"content-type": "video/mp4"})
    )
    with pytest.raises(MediaFound):
        _fetch(_settings(tmp_path), "https://example.com/a/watch")
    assert route.called


@pytest.mark.parametrize(
    "markup",
    [
        '<iframe src="https://www.youtube.com/embed/abc"></iframe>',
        '<meta property="og:video" content="https://www.youtube.com/embed/abc">',
    ],
)
@respx.mock
def test_thin_page_embedding_an_allow_listed_host_goes_to_the_downloader(tmp_path, markup):
    respx.get("https://example.com/watch").mock(return_value=_html_response(_html_page(markup)))
    with pytest.raises(MediaFound) as caught:
        _fetch(_settings(tmp_path), "https://example.com/watch")
    assert caught.value.url == "https://www.youtube.com/embed/abc"
    assert caught.value.result is None


@pytest.mark.parametrize(
    "markup",
    [
        '<iframe src="https://www.youtube.com/favicon.ico"></iframe>',
        '<iframe src="https://www.youtube.com/s/player.js"></iframe>',
        '<iframe src="https://www.youtube.com/img/logo.png"></iframe>',
    ],
)
@respx.mock
def test_a_static_file_on_an_allow_listed_host_is_not_an_embed(tmp_path, markup):
    description = "A real description of this page that is well over the sixty character floor."
    page = _html_page(markup, head=f'<meta name="description" content="{description}">')
    respx.get("https://example.com/watch").mock(return_value=_html_response(page))

    result = _fetch(_settings(tmp_path), "https://example.com/watch")

    assert result.backend == "page-metadata"
    assert description in result.text


@respx.mock
def test_embed_keeps_the_thin_page_text_as_context(tmp_path):
    body = "<p>A short intro above the video on this page, a real sentence.</p>"
    body += '<iframe src="https://www.youtube.com/embed/abc"></iframe>'
    respx.get("https://example.com/watch").mock(return_value=_html_response(_html_page(body)))
    with pytest.raises(MediaFound) as caught:
        _fetch(_settings(tmp_path), "https://example.com/watch")
    assert "short intro" in caught.value.context


@respx.mock
def test_blog_post_with_real_text_and_an_embed_stays_text(tmp_path):
    body = f'<article><p>{_LONG_TEXT}</p><iframe src="https://www.youtube.com/embed/abc"></iframe></article>'
    respx.get("https://example.com/post").mock(return_value=_html_response(_html_page(body)))
    result = _fetch(_settings(tmp_path), "https://example.com/post")
    assert "real paragraph" in result.text
    assert result.backend == "html-fetch"


@respx.mock
def test_iframe_to_an_unlisted_host_is_not_followed(tmp_path):
    respx.get("https://example.com/watch").mock(
        return_value=_html_response(
            _html_page('<iframe src="https://ads.example.net/frame"></iframe>')
        )
    )
    frame = respx.get("https://ads.example.net/frame")
    with pytest.raises(TextFetchError):
        _fetch(_settings(tmp_path), "https://example.com/watch")
    assert not frame.called


@respx.mock
def test_html_og_video_gets_one_hop_looking_for_a_file(tmp_path):
    respx.get("https://example.com/watch").mock(
        return_value=_html_response(
            _html_page(head='<meta property="og:video" content="https://player.example.net/e/1">')
        )
    )
    respx.get("https://player.example.net/e/1").mock(
        return_value=_html_response(_html_page('<video src="/files/1.webm"></video>'))
    )
    respx.get("https://player.example.net/files/1.webm").mock(
        return_value=httpx.Response(200, content=b"\x00w", headers={"content-type": "video/webm"})
    )
    with pytest.raises(MediaFound) as caught:
        _fetch(_settings(tmp_path), "https://example.com/watch", "cid-hop")
    assert caught.value.result is not None
    assert caught.value.result.media_paths[0].endswith("file.webm")


@respx.mock
def test_hop_goes_only_one_level(tmp_path):
    respx.get("https://example.com/watch").mock(
        return_value=_html_response(
            _html_page(head='<meta property="og:video" content="https://player.example.net/e/1">')
        )
    )
    respx.get("https://player.example.net/e/1").mock(
        return_value=_html_response(
            _html_page(head='<meta property="og:video" content="https://deeper.example.net/e/2">')
        )
    )
    deeper = respx.get("https://deeper.example.net/e/2")
    with pytest.raises(TextFetchError):
        _fetch(_settings(tmp_path), "https://example.com/watch")
    assert not deeper.called


def test_embed_to_a_private_host_is_refused_by_the_guard(tmp_path, fake_dns):
    fake_dns({"example.com": ["93.184.216.34"], "cdn.internal.example": ["10.0.0.9"]})
    with respx.mock:
        respx.get("https://example.com/watch").mock(
            return_value=_html_response(
                _html_page('<video src="http://cdn.internal.example/a.mp4"></video>')
            )
        )
        secret = respx.get("http://cdn.internal.example/a.mp4")
        with pytest.raises(TextFetchError):
            _fetch(_settings(tmp_path), "https://example.com/watch")
        assert not secret.called


def test_hop_to_a_private_host_is_refused_by_the_guard(tmp_path, fake_dns):
    fake_dns({"example.com": ["93.184.216.34"], "player.internal.example": ["192.168.1.9"]})
    with respx.mock:
        respx.get("https://example.com/watch").mock(
            return_value=_html_response(
                _html_page(
                    head='<meta property="og:video" content="http://player.internal.example/e">'
                )
            )
        )
        secret = respx.get("http://player.internal.example/e")
        with pytest.raises(TextFetchError):
            _fetch(_settings(tmp_path), "https://example.com/watch")
        assert not secret.called


# --- HLS --------------------------------------------------------------------

_MASTER = (
    "#EXTM3U\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=900000\nhigh/index.m3u8\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=100000\nlow/index.m3u8\n"
)
_MEDIA = "#EXTM3U\n#EXTINF:4,\nseg0.ts\n#EXTINF:4,\nseg1.ts\n#EXT-X-ENDLIST\n"


def _mock_hls():
    respx.get("https://cdn.example.com/v/master.m3u8").mock(
        return_value=httpx.Response(
            200, text=_MASTER, headers={"content-type": "application/vnd.apple.mpegurl"}
        )
    )
    respx.get("https://cdn.example.com/v/low/index.m3u8").mock(
        return_value=httpx.Response(200, text=_MEDIA)
    )
    respx.get("https://cdn.example.com/v/low/seg0.ts").mock(
        return_value=httpx.Response(200, content=b"\x47one")
    )
    respx.get("https://cdn.example.com/v/low/seg1.ts").mock(
        return_value=httpx.Response(200, content=b"\x47two")
    )


@respx.mock
def test_hls_url_picks_lowest_bandwidth_and_concatenates_segments(tmp_path):
    settings = _settings(tmp_path)
    _mock_hls()
    high = respx.get("https://cdn.example.com/v/high/index.m3u8")
    with pytest.raises(MediaFound) as caught:
        _fetch(settings, "https://cdn.example.com/v/master.m3u8", "cid-h")
    result = caught.value.result
    assert result is not None
    assert result.platform == "hls"
    assert result.media_type is MediaType.VIDEO
    assert (settings.tmp_dir / "cid-h" / "file.ts").read_bytes() == b"\x47one\x47two"
    assert not high.called


@pytest.mark.parametrize(
    "markup",
    [
        '<script>var src = "https://cdn.example.com/v/master.m3u8";</script>',
        '<script>{"src":"https:\\/\\/cdn.example.com\\/v\\/master.m3u8"}</script>',
    ],
)
@respx.mock
def test_thin_page_with_an_m3u8_downloads_it(tmp_path, markup):
    _mock_hls()
    respx.get("https://example.com/watch").mock(return_value=_html_response(_html_page(markup)))
    with pytest.raises(MediaFound) as caught:
        _fetch(_settings(tmp_path), "https://example.com/watch", "cid-h2")
    assert caught.value.result is not None


@respx.mock
def test_hls_fmp4_prepends_the_init_segment(tmp_path):
    settings = _settings(tmp_path)
    playlist = '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:4,\nseg0.m4s\n#EXT-X-ENDLIST\n'
    respx.get("https://cdn.example.com/m.m3u8").mock(
        return_value=httpx.Response(200, text=playlist)
    )
    respx.get("https://cdn.example.com/init.mp4").mock(
        return_value=httpx.Response(200, content=b"INIT")
    )
    respx.get("https://cdn.example.com/seg0.m4s").mock(
        return_value=httpx.Response(200, content=b"SEG")
    )
    with pytest.raises(MediaFound):
        _fetch(settings, "https://cdn.example.com/m.m3u8", "cid-f")
    assert (settings.tmp_dir / "cid-f" / "file.mp4").read_bytes() == b"INITSEG"


@respx.mock
def test_hls_uses_the_separate_audio_rendition_when_the_variant_has_one(tmp_path):
    settings = _settings(tmp_path)
    master = (
        "#EXTM3U\n"
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="en",URI="audio/index.m3u8"\n'
        '#EXT-X-STREAM-INF:BANDWIDTH=100000,AUDIO="aud"\nvideo/index.m3u8\n'
    )
    respx.get("https://cdn.example.com/m.m3u8").mock(return_value=httpx.Response(200, text=master))
    respx.get("https://cdn.example.com/audio/index.m3u8").mock(
        return_value=httpx.Response(200, text="#EXTM3U\n#EXTINF:4,\na.aac\n#EXT-X-ENDLIST\n")
    )
    respx.get("https://cdn.example.com/audio/a.aac").mock(
        return_value=httpx.Response(200, content=b"AUD")
    )
    video = respx.get("https://cdn.example.com/video/index.m3u8")
    with pytest.raises(MediaFound):
        _fetch(settings, "https://cdn.example.com/m.m3u8", "cid-a")
    assert (settings.tmp_dir / "cid-a" / "file.aac").read_bytes() == b"AUD"
    assert not video.called


@pytest.mark.parametrize(
    "key",
    [
        '#EXT-X-KEY:METHOD=AES-128,URI="k"',
        '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="k"',
        '#EXT-X-SESSION-KEY:METHOD=SAMPLE-AES,URI="k"',
    ],
)
@respx.mock
def test_hls_with_encryption_is_refused(tmp_path, key):
    playlist = f"#EXTM3U\n{key}\n#EXTINF:4,\nseg0.ts\n#EXT-X-ENDLIST\n"
    respx.get("https://cdn.example.com/m.m3u8").mock(
        return_value=httpx.Response(200, text=playlist)
    )
    segment = respx.get("https://cdn.example.com/seg0.ts")
    with pytest.raises(TextFetchError, match="encrypted"):
        _fetch(_settings(tmp_path), "https://cdn.example.com/m.m3u8")
    assert not segment.called


@respx.mock
def test_hls_key_method_none_is_fine(tmp_path):
    playlist = "#EXTM3U\n#EXT-X-KEY:METHOD=NONE\n#EXTINF:4,\nseg0.ts\n#EXT-X-ENDLIST\n"
    respx.get("https://cdn.example.com/m.m3u8").mock(
        return_value=httpx.Response(200, text=playlist)
    )
    respx.get("https://cdn.example.com/seg0.ts").mock(
        return_value=httpx.Response(200, content=b"\x47x")
    )
    with pytest.raises(MediaFound):
        _fetch(_settings(tmp_path), "https://cdn.example.com/m.m3u8")


@respx.mock
def test_hls_refused_on_a_thin_page_falls_back_to_metadata(tmp_path):
    playlist = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k"\n#EXTINF:4,\ns.ts\n#EXT-X-ENDLIST\n'
    respx.get("https://cdn.example.com/m.m3u8").mock(
        return_value=httpx.Response(200, text=playlist)
    )
    description = "A long enough description of this protected video for the note to be useful."
    respx.get("https://example.com/watch").mock(
        return_value=_html_response(
            _html_page(
                '<script>var s="https://cdn.example.com/m.m3u8";</script>',
                head=f'<title>Video</title><meta name="description" content="{description}">',
            )
        )
    )
    result = _fetch(_settings(tmp_path), "https://example.com/watch")
    assert result.backend == "page-metadata"
    assert description in result.text


@respx.mock
def test_hls_byte_ranges_fetch_the_one_file_once(tmp_path):
    # A real shape (livid.com): every segment is a byte range of one fMP4 file.
    playlist = (
        "#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-PLAYLIST-TYPE:VOD\n"
        '#EXT-X-MAP:URI="whole.mp4",BYTERANGE="10@0"\n'
        "#EXTINF:6.0,\n#EXT-X-BYTERANGE:20@10\nwhole.mp4\n"
        "#EXTINF:6.0,\n#EXT-X-BYTERANGE:20@30\nwhole.mp4\n#EXT-X-ENDLIST\n"
    )
    respx.get("https://cdn.example.com/v/playlist.m3u8").mock(
        return_value=httpx.Response(
            200, text=playlist, headers={"content-type": "application/vnd.apple.mpegurl"}
        )
    )
    whole = respx.get("https://cdn.example.com/v/whole.mp4").mock(
        return_value=httpx.Response(200, content=b"init+all+the+segments")
    )

    with pytest.raises(MediaFound) as caught:
        GenericHtmlFetcher(_settings(tmp_path)).fetch(
            "https://cdn.example.com/v/playlist.m3u8", "cid"
        )

    assert whole.call_count == 1
    found = caught.value.result
    assert found is not None
    saved = Path(found.media_paths[0])
    assert saved.suffix == ".mp4"
    assert saved.read_bytes() == b"init+all+the+segments"


@respx.mock
def test_hls_honors_the_media_cap_across_segments(tmp_path):
    settings = _settings(tmp_path, max_media_mb=1)
    playlist = "#EXTM3U\n#EXTINF:4,\nseg0.ts\n#EXTINF:4,\nseg1.ts\n#EXT-X-ENDLIST\n"
    respx.get("https://cdn.example.com/m.m3u8").mock(
        return_value=httpx.Response(200, text=playlist)
    )
    respx.get("https://cdn.example.com/seg0.ts").mock(
        return_value=httpx.Response(200, content=b"\x47" * (700 * 1024))
    )
    respx.get("https://cdn.example.com/seg1.ts").mock(
        return_value=httpx.Response(200, content=b"\x47" * (700 * 1024))
    )
    with pytest.raises(TextFetchError, match="1 MB"):
        _fetch(settings, "https://cdn.example.com/m.m3u8", "cid-cap")
    assert not list((settings.tmp_dir / "cid-cap").glob("*"))


def test_hls_segment_on_a_private_host_is_refused_by_the_guard(tmp_path, fake_dns):
    fake_dns({"cdn.example.com": ["93.184.216.34"], "seg.internal.example": ["127.0.0.1"]})
    playlist = "#EXTM3U\n#EXTINF:4,\nhttp://seg.internal.example/a.ts\n#EXT-X-ENDLIST\n"
    with respx.mock:
        respx.get("https://cdn.example.com/m.m3u8").mock(
            return_value=httpx.Response(200, text=playlist)
        )
        secret = respx.get("http://seg.internal.example/a.ts")
        with pytest.raises(TextFetchError, match="private or local"):
            _fetch(_settings(tmp_path), "https://cdn.example.com/m.m3u8")
        assert not secret.called


# --- metadata fallback ------------------------------------------------------

_DESCRIPTION = "This is what the site says about itself, at least sixty characters long."


def _meta_page(description=_DESCRIPTION, title="Example App"):
    return _html_page(
        '<div id="root"></div>',
        head=f'<title>{title}</title><meta property="og:description" content="{description}">',
    )


@respx.mock
def test_metadata_used_when_render_is_disabled(tmp_path):
    respx.get("https://example.com/app").mock(return_value=_html_response(_meta_page()))
    result = _fetch(_settings(tmp_path), "https://example.com/app")
    assert result.text.startswith("[Only the page's title and description were available]")
    assert "Example App" in result.text
    assert _DESCRIPTION in result.text
    assert result.backend == "page-metadata"


@respx.mock
def test_metadata_used_when_render_fails(tmp_path, monkeypatch):
    fetcher = GenericHtmlFetcher(_settings(tmp_path, render=True))

    def _fail(url, content_id):
        raise TextFetchError("failed to render")

    monkeypatch.setattr(fetcher._rendered, "fetch", _fail)
    respx.get("https://example.com/app").mock(return_value=_html_response(_meta_page()))
    result = fetcher.fetch("https://example.com/app", "cid")
    assert result.backend == "page-metadata"


@respx.mock
def test_no_metadata_after_a_render_challenge(tmp_path, monkeypatch):
    from reel_pipeline.text_fetcher import _raise_if_challenge_or_login_wall

    fetcher = GenericHtmlFetcher(_settings(tmp_path, render=True))

    def _challenged(url, content_id):
        _raise_if_challenge_or_login_wall("Just a moment...", url)

    monkeypatch.setattr(fetcher._rendered, "fetch", _challenged)
    respx.get("https://example.com/app").mock(return_value=_html_response(_meta_page()))
    with pytest.raises(TextFetchError, match="bot-detection"):
        fetcher.fetch("https://example.com/app", "cid")


@pytest.mark.parametrize(
    "description",
    ["Too short", "Please log in to see this page, which has a long enough description here."],
)
@respx.mock
def test_short_or_login_metadata_is_rejected(tmp_path, description):
    respx.get("https://example.com/app").mock(
        return_value=_html_response(_meta_page(description=description))
    )
    with pytest.raises(TextFetchError, match="client-side"):
        _fetch(_settings(tmp_path), "https://example.com/app")


@respx.mock
def test_challenge_title_is_rejected_as_metadata(tmp_path):
    respx.get("https://example.com/app").mock(
        return_value=_html_response(_meta_page(title="Just a moment..."))
    )
    with pytest.raises(TextFetchError):
        _fetch(_settings(tmp_path), "https://example.com/app")


# --- non-200 ladder ---------------------------------------------------------


@pytest.mark.parametrize("status", [404, 410])
@respx.mock
def test_dead_page_goes_to_wayback_at_once(tmp_path, status):
    respx.get("https://example.com/gone").mock(return_value=httpx.Response(status))
    respx.get(url__startswith="https://archive.org/wayback/available").mock(
        return_value=_wayback_answer()
    )
    snapshot = respx.get("https://web.archive.org/web/20240102030405id_/https://example.com/gone")
    snapshot.mock(return_value=_html_response(_html_page(f"<p>{_LONG_TEXT}</p>")))
    result = _fetch(_settings(tmp_path), "https://example.com/gone")
    assert result.text.startswith(
        f"[Archived copy from 2024-01-02; the live page returned HTTP {status}]"
    )
    assert "real paragraph" in result.text
    assert result.backend == "wayback"


@respx.mock
def test_no_snapshot_keeps_the_status_in_the_error(tmp_path):
    respx.get("https://example.com/gone").mock(return_value=httpx.Response(404))
    respx.get(url__startswith="https://archive.org/wayback/available").mock(
        return_value=httpx.Response(200, json={"archived_snapshots": {}})
    )
    with pytest.raises(TextFetchError, match="404") as caught:
        _fetch(_settings(tmp_path), "https://example.com/gone")
    assert not isinstance(caught.value, ArchiveEligibleError)


@respx.mock
def test_snapshot_that_was_not_a_200_is_not_used(tmp_path):
    respx.get("https://example.com/gone").mock(return_value=httpx.Response(404))
    respx.get(url__startswith="https://archive.org/wayback/available").mock(
        return_value=_wayback_answer(status="302")
    )
    snapshot = respx.get(url__startswith="https://web.archive.org/")
    with pytest.raises(TextFetchError, match="404"):
        _fetch(_settings(tmp_path), "https://example.com/gone")
    assert not snapshot.called


@pytest.mark.parametrize(
    "query",
    [
        "token=abc",
        "api_key=abc",
        "apikey=abc",
        "secret=abc",
        "X-Amz-Signature=abc",
        "auth=abc",
        "sig=abc",
        "oauth_sig=abc",
        "sig_v1=abc",
        "sigV1=abc",
        "sig2=abc",
        "key_id=abc",
        "accessKey=abc",
        "resourcekey=abc",
        "X-Amz-Credential=abc",
        "jwt=abc",
        "pwd=abc",
        "session_id=abc",
        "sessionid=abc",
    ],
)
@respx.mock
def test_private_looking_links_are_never_sent_to_the_archive(tmp_path, query):
    url = f"https://example.com/gone?{query}"
    respx.get(url).mock(return_value=httpx.Response(404))
    lookup = respx.get(url__startswith="https://archive.org/wayback/available")
    with pytest.raises(TextFetchError, match="404"):
        _fetch(_settings(tmp_path), url)
    assert not lookup.called


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/gone#access_token=abc",
        "https://example.com/gone#/callback?sig_v1=abc",
        "https://someone:hunter2@example.com/gone",
    ],
)
@respx.mock
def test_a_fragment_or_login_in_the_link_also_keeps_it_from_the_archive(tmp_path, url):
    respx.get("https://example.com/gone").mock(return_value=httpx.Response(404))
    lookup = respx.get(url__startswith="https://archive.org/wayback/available")
    with pytest.raises(TextFetchError, match="404"):
        _fetch(_settings(tmp_path), url)
    assert not lookup.called


@respx.mock
def test_innocent_query_params_can_go_to_the_archive(tmp_path):
    url = (
        "https://example.com/gone?monkey=1&design=2&keyword=3&signal=4&author=5&sigma=6&hotkey=7"
        "&tokenizer=8&tokenize=9"
    )
    respx.get(url).mock(return_value=httpx.Response(404))
    lookup = respx.get(url__startswith="https://archive.org/wayback/available").mock(
        return_value=httpx.Response(200, json={"archived_snapshots": {}})
    )
    with pytest.raises(TextFetchError, match="404"):
        _fetch(_settings(tmp_path), url)
    assert lookup.called


@pytest.mark.parametrize("status", [401, 403, 429, 451, 400])
@respx.mock
def test_refusals_do_not_escalate_to_archive_or_render(tmp_path, monkeypatch, status):
    fetcher = GenericHtmlFetcher(_settings(tmp_path, render=True))
    monkeypatch.setattr(
        fetcher._rendered,
        "fetch",
        lambda *a: pytest.fail("a refusal must never escalate to the render"),
    )
    respx.get("https://example.com/no").mock(return_value=httpx.Response(status))
    lookup = respx.get(url__startswith="https://archive.org/wayback/available")
    with pytest.raises(TextFetchError, match=str(status)) as caught:
        fetcher.fetch("https://example.com/no", "cid")
    assert not lookup.called
    assert not isinstance(caught.value, ArchiveEligibleError)


@respx.mock
def test_challenge_page_does_not_escalate_to_archive_or_metadata(tmp_path):
    respx.get("https://example.com/c").mock(
        return_value=_html_response(_html_page("<p>Just a moment... checking your browser</p>"))
    )
    lookup = respx.get(url__startswith="https://archive.org/wayback/available")
    with pytest.raises(TextFetchError) as caught:
        _fetch(_settings(tmp_path), "https://example.com/c")
    assert not lookup.called
    assert not isinstance(caught.value, ArchiveEligibleError)


@pytest.mark.parametrize("status", [500, 502, 503])
@respx.mock
def test_server_errors_are_archive_eligible_but_not_archived_at_once(tmp_path, monkeypatch, status):
    fetcher = GenericHtmlFetcher(_settings(tmp_path, render=True))
    monkeypatch.setattr(
        fetcher._rendered, "fetch", lambda *a: pytest.fail("no render on a non-200")
    )
    respx.get("https://example.com/down").mock(return_value=httpx.Response(status))
    lookup = respx.get(url__startswith="https://archive.org/wayback/available")
    with pytest.raises(ArchiveEligibleError, match=str(status)):
        fetcher.fetch("https://example.com/down", "cid")
    assert not lookup.called


@respx.mock
def test_connection_errors_are_archive_eligible(tmp_path):
    respx.get("https://example.com/down").mock(side_effect=httpx.ConnectError("nope"))
    with pytest.raises(ArchiveEligibleError, match="failed to fetch page"):
        _fetch(_settings(tmp_path), "https://example.com/down")


@respx.mock
def test_fetch_archived_labels_the_reason(tmp_path):
    respx.get(url__startswith="https://archive.org/wayback/available").mock(
        return_value=_wayback_answer()
    )
    respx.get("https://web.archive.org/web/20240102030405id_/https://example.com/down").mock(
        return_value=_html_response(_html_page(f"<p>{_LONG_TEXT}</p>"))
    )
    result = DispatchingTextFetcher(_settings(tmp_path)).fetch_archived(
        "https://example.com/down", "cid", "returned HTTP 503"
    )
    assert result.text.startswith(
        "[Archived copy from 2024-01-02; the live page returned HTTP 503]"
    )
    assert result.backend == "wayback"


class _RawBody(httpx.SyncByteStream):
    """Bytes as sent, so the mock doesn't decode a body that only claims to be gzip."""

    def __init__(self, body: bytes) -> None:
        self.body = body

    def __iter__(self):
        yield self.body


@pytest.mark.parametrize("compress", [False, True], ids=["plain-bytes", "real-gzip"])
@respx.mock
def test_archived_copy_survives_a_replayed_gzip_header(tmp_path, compress):
    # Seen live: an id_ snapshot repeats the original Content-Encoding over plain bytes.
    body = _html_page(f"<p>{_LONG_TEXT}</p>").encode()
    respx.get(url__startswith="https://archive.org/wayback/available").mock(
        return_value=_wayback_answer()
    )
    respx.get("https://web.archive.org/web/20240102030405id_/https://example.com/down").mock(
        side_effect=lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8", "Content-Encoding": "gzip"},
            stream=_RawBody(gzip.compress(body) if compress else body),
        )
    )
    result = DispatchingTextFetcher(_settings(tmp_path)).fetch_archived(
        "https://example.com/down", "cid", "returned HTTP 404"
    )
    assert result.backend == "wayback"
    assert _LONG_TEXT[:40] in result.text


@respx.mock
def test_corrupt_gzip_in_an_archived_copy_is_a_clear_error(tmp_path):
    respx.get(url__startswith="https://archive.org/wayback/available").mock(
        return_value=_wayback_answer()
    )
    respx.get("https://web.archive.org/web/20240102030405id_/https://example.com/down").mock(
        side_effect=lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            stream=_RawBody(b"\x1f\x8b" + b"not really gzip" * 10),
        )
    )
    with pytest.raises(TextFetchError, match="corrupt"):
        DispatchingTextFetcher(_settings(tmp_path)).fetch_archived(
            "https://example.com/down", "cid", "returned HTTP 404"
        )


@respx.mock
def test_archived_copy_that_is_itself_a_challenge_is_refused(tmp_path):
    respx.get("https://example.com/gone").mock(return_value=httpx.Response(404))
    respx.get(url__startswith="https://archive.org/wayback/available").mock(
        return_value=_wayback_answer()
    )
    respx.get("https://web.archive.org/web/20240102030405id_/https://example.com/gone").mock(
        return_value=_html_response(_html_page("<p>Please log in to continue reading this.</p>"))
    )
    with pytest.raises(TextFetchError, match="404"):
        _fetch(_settings(tmp_path), "https://example.com/gone")


@respx.mock
def test_archived_app_shell_is_not_rendered(tmp_path, monkeypatch):
    fetcher = GenericHtmlFetcher(_settings(tmp_path, render=True))
    monkeypatch.setattr(fetcher._rendered, "fetch", lambda *a: pytest.fail("no render on archive"))
    respx.get("https://example.com/gone").mock(return_value=httpx.Response(404))
    respx.get(url__startswith="https://archive.org/wayback/available").mock(
        return_value=_wayback_answer()
    )
    respx.get("https://web.archive.org/web/20240102030405id_/https://example.com/gone").mock(
        return_value=_html_response(_html_page('<div id="root"></div>'))
    )
    with pytest.raises(TextFetchError, match="404"):
        fetcher.fetch("https://example.com/gone", "cid")


@respx.mock
def test_archived_copy_that_is_also_gone_does_not_loop(tmp_path):
    respx.get("https://example.com/gone").mock(return_value=httpx.Response(404))
    lookup = respx.get(url__startswith="https://archive.org/wayback/available").mock(
        return_value=_wayback_answer()
    )
    respx.get("https://web.archive.org/web/20240102030405id_/https://example.com/gone").mock(
        return_value=httpx.Response(404)
    )
    with pytest.raises(TextFetchError, match="404"):
        _fetch(_settings(tmp_path), "https://example.com/gone")
    assert lookup.call_count == 1


@respx.mock
def test_archived_media_keeps_the_label_in_its_context(tmp_path):
    respx.get("https://example.com/v.mp4").mock(return_value=httpx.Response(404))
    respx.get(url__startswith="https://archive.org/wayback/available").mock(
        return_value=_wayback_answer()
    )
    respx.get("https://web.archive.org/web/20240102030405id_/https://example.com/v.mp4").mock(
        return_value=httpx.Response(200, content=b"\x00v", headers={"content-type": "video/mp4"})
    )
    with pytest.raises(MediaFound) as caught:
        _fetch(_settings(tmp_path), "https://example.com/v.mp4")
    assert "[Archived copy from 2024-01-02" in caught.value.context


def test_archive_redirect_to_a_private_host_is_refused_by_the_guard(tmp_path, fake_dns):
    fake_dns(
        {
            "example.com": ["93.184.216.34"],
            "archive.org": ["207.241.224.2"],
            "web.archive.org": ["207.241.224.2"],
            "internal.example": ["10.1.1.1"],
        }
    )
    with respx.mock:
        respx.get("https://example.com/gone").mock(return_value=httpx.Response(404))
        respx.get(url__startswith="https://archive.org/wayback/available").mock(
            return_value=_wayback_answer()
        )
        respx.get("https://web.archive.org/web/20240102030405id_/https://example.com/gone").mock(
            return_value=httpx.Response(302, headers={"location": "http://internal.example/x"})
        )
        secret = respx.get("http://internal.example/x")
        with pytest.raises(TextFetchError, match="404"):
            _fetch(_settings(tmp_path), "https://example.com/gone")
        assert not secret.called


@respx.mock
def test_text_path_sends_no_cookies(tmp_path, monkeypatch):
    monkeypatch.setenv("REEL_YTDLP_COOKIES_FILE", "ignored.txt")
    route = respx.get("https://example.com/a.txt").mock(
        return_value=httpx.Response(200, text="hello", headers={"content-type": "text/plain"})
    )
    _fetch(_settings(tmp_path), "https://example.com/a.txt")
    assert "cookie" not in {k.lower() for k in route.calls.last.request.headers}


# --- dispatch ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["https://gist.github.com/someone/abc123", "https://docs.github.com/en/rest"],
)
def test_github_subdomains_go_to_the_generic_fetcher(tmp_path, monkeypatch, url):
    dispatcher = DispatchingTextFetcher(_settings(tmp_path))
    monkeypatch.setattr(dispatcher._github, "fetch", lambda *a: pytest.fail("not the repo API"))
    monkeypatch.setattr(dispatcher._generic_html, "fetch", lambda u, c: "generic")
    assert dispatcher.fetch(url, "cid") == "generic"


@pytest.mark.parametrize(
    ("attribute", "url"),
    [
        ("_github", "https://github.com/a/b/issues/1"),
        ("_notion", "https://someone.notion.site/"),
    ],
)
def test_github_and_notion_failures_try_generic(tmp_path, monkeypatch, attribute, url):
    dispatcher = DispatchingTextFetcher(_settings(tmp_path))

    def _fail(*args):
        raise TextFetchError("api said no")

    monkeypatch.setattr(getattr(dispatcher, attribute), "fetch", _fail)
    monkeypatch.setattr(dispatcher._generic_html, "fetch", lambda u, c: "generic")
    assert dispatcher.fetch(url, "cid") == "generic"


def test_both_failures_keep_both_messages_and_the_second_type(tmp_path, monkeypatch):
    dispatcher = DispatchingTextFetcher(_settings(tmp_path))

    def _api(*args):
        raise TextFetchError("api said no")

    def _generic(*args):
        raise ArchiveEligibleError("page returned HTTP 503")

    monkeypatch.setattr(dispatcher._github, "fetch", _api)
    monkeypatch.setattr(dispatcher._generic_html, "fetch", _generic)
    with pytest.raises(ArchiveEligibleError) as caught:
        dispatcher.fetch("https://github.com/a/b", "cid")
    assert "api said no" in str(caught.value)
    assert "503" in str(caught.value)


def test_media_found_from_the_generic_fallback_passes_through(tmp_path, monkeypatch):
    dispatcher = DispatchingTextFetcher(_settings(tmp_path))

    def _api(*args):
        raise TextFetchError("api said no")

    def _generic(*args):
        raise MediaFound(url="https://www.youtube.com/watch?v=1")

    monkeypatch.setattr(dispatcher._github, "fetch", _api)
    monkeypatch.setattr(dispatcher._generic_html, "fetch", _generic)
    with pytest.raises(MediaFound):
        dispatcher.fetch("https://github.com/a/b", "cid")


def test_drive_failures_do_not_try_generic(tmp_path, monkeypatch):
    dispatcher = DispatchingTextFetcher(_settings(tmp_path))

    def _fail(*args):
        raise TextFetchError("drive said no")

    monkeypatch.setattr(dispatcher._drive, "fetch", _fail)
    monkeypatch.setattr(dispatcher._generic_html, "fetch", lambda *a: pytest.fail("not for Drive"))
    with pytest.raises(TextFetchError, match="drive said no"):
        dispatcher.fetch("https://drive.google.com/file/d/abc123/view", "cid")


# --- Drive confirm ----------------------------------------------------------

_DRIVE_FORM = (
    "<!doctype html><html><body><h1>Virus scan warning</h1>"
    "<p>big.pdf is too large for Google to scan for viruses.</p>"
    '<form id="download-form" action="{action}" method="get">'
    '<input type="submit" value="Download anyway">'
    '<input type="hidden" name="id" value="big123">'
    '<input type="hidden" name="export" value="download">'
    '<input type="hidden" name="confirm" value="t">'
    '<input type="hidden" name="uuid" value="u-1">'
    "</form></body></html>"
)
_CONFIRM_URL = (
    "https://drive.usercontent.google.com/download?id=big123&export=download&confirm=t&uuid=u-1"
)


@respx.mock
def test_drive_follows_the_virus_scan_confirm_form(tmp_path):
    settings = _settings(tmp_path)
    data = _pdf(text="Past the interstitial")
    respx.get("https://drive.google.com/uc?export=download&id=big123").mock(
        return_value=_html_response(
            _DRIVE_FORM.format(action="https://drive.usercontent.google.com/download")
        )
    )
    respx.get(_CONFIRM_URL).mock(
        return_value=httpx.Response(
            200,
            content=data,
            headers={"content-disposition": 'attachment; filename="big.pdf"'},
        )
    )
    result = DriveFetcher(settings).fetch("https://drive.google.com/file/d/big123/view", "cid")
    assert (settings.vault_dir / "attachments" / "big.pdf").read_bytes() == data
    assert "Past the interstitial" in result.text
    assert result.backend == "drive-attachment"


@respx.mock
def test_drive_confirm_form_pointing_off_google_keeps_the_old_error(tmp_path):
    respx.get("https://drive.google.com/uc?export=download&id=big123").mock(
        return_value=_html_response(_DRIVE_FORM.format(action="https://evil.example.com/download"))
    )
    evil = respx.get(url__startswith="https://evil.example.com/")
    with pytest.raises(TextFetchError, match="too large"):
        DriveFetcher(_settings(tmp_path)).fetch(
            "https://drive.google.com/file/d/big123/view", "cid"
        )
    assert not evil.called


@respx.mock
def test_drive_confirm_that_is_still_the_interstitial_keeps_the_old_error(tmp_path):
    form = _DRIVE_FORM.format(action="https://drive.usercontent.google.com/download")
    respx.get("https://drive.google.com/uc?export=download&id=big123").mock(
        return_value=_html_response(form)
    )
    respx.get(_CONFIRM_URL).mock(return_value=_html_response(form))
    with pytest.raises(TextFetchError, match="too large"):
        DriveFetcher(_settings(tmp_path)).fetch(
            "https://drive.google.com/file/d/big123/view", "cid"
        )


def test_drive_confirm_redirect_to_a_private_host_is_refused_by_the_guard(tmp_path, fake_dns):
    fake_dns(
        {
            "drive.google.com": ["142.250.1.1"],
            "drive.usercontent.google.com": ["142.250.1.2"],
            "internal.example": ["10.1.1.1"],
        }
    )
    with respx.mock:
        respx.get("https://drive.google.com/uc?export=download&id=big123").mock(
            return_value=_html_response(
                _DRIVE_FORM.format(action="https://drive.usercontent.google.com/download")
            )
        )
        respx.get(_CONFIRM_URL).mock(
            return_value=httpx.Response(302, headers={"location": "http://internal.example/x"})
        )
        secret = respx.get("http://internal.example/x")
        with pytest.raises(TextFetchError, match="private or local"):
            DriveFetcher(_settings(tmp_path)).fetch(
                "https://drive.google.com/file/d/big123/view", "cid"
            )
        assert not secret.called


@respx.mock
def test_drive_file_over_the_cap_is_a_clear_error(tmp_path):
    settings = _settings(tmp_path, max_document_mb=1)
    respx.get("https://drive.google.com/uc?export=download&id=abc789").mock(
        return_value=httpx.Response(200, content=b"\xff" * (1024 * 1024 + 1))
    )
    with pytest.raises(TextFetchError, match="1 MB"):
        DriveFetcher(settings).fetch("https://drive.google.com/file/d/abc789/view", "cid")


@respx.mock
def test_drive_folder_with_a_scanned_pdf_keeps_its_embed_in_the_section(tmp_path):
    settings = _settings(tmp_path)
    listing = (
        '<div class="flip-entry" id="entry-abc123"><a href="https://drive.google.com/file/d/abc123/view">'
        '<div class="flip-entry-title">scan.pdf</div></a></div>'
    )
    respx.get("https://drive.google.com/embeddedfolderview?id=folder1").mock(
        return_value=httpx.Response(200, text=listing)
    )
    respx.get("https://drive.google.com/uc?export=download&id=abc123").mock(
        return_value=httpx.Response(
            200,
            content=_pdf(image=_JPEG),
            headers={"content-disposition": 'attachment; filename="scan.pdf"'},
        )
    )
    result = DriveFetcher(settings).fetch("https://drive.google.com/drive/folders/folder1", "cid")
    assert "## scan.pdf" in result.text
    assert "![[scan.pdf]]" in result.text


# --- Notion pagination ------------------------------------------------------

_NOTION_ID = "2e22de6b770e4166be301490f6ffd420"
_NOTION_DASHED = "2e22de6b-770e-4166-be30-1490f6ffd420"


def _notion_block(block_id, title, content=()):
    return {
        block_id: {
            "value": {
                "id": block_id,
                "type": "text",
                "properties": {"title": [[title]]},
                "content": list(content),
            }
        }
    }


def _notion_chunk(blocks, stack=()):
    return httpx.Response(
        200, json={"cursor": {"stack": list(stack)}, "recordMap": {"block": blocks}}
    )


@respx.mock
def test_notion_follows_the_cursor_and_merges_chunks(tmp_path):
    route = respx.post("https://app.notion.com/api/v3/loadPageChunk")
    route.side_effect = [
        _notion_chunk(
            {**_notion_block(_NOTION_DASHED, "Root", ["b1", "b2"]), **_notion_block("b1", "One")},
            stack=[[{"id": "x"}]],
        ),
        _notion_chunk(_notion_block("b2", "Two")),
    ]
    result = NotionFetcher(_settings(tmp_path)).fetch(
        f"https://example.notion.site/Page-{_NOTION_ID}", "cid"
    )
    assert "One" in result.text
    assert "Two" in result.text
    assert route.call_count == 2
    second = json.loads(route.calls[1].request.content)
    assert second["chunkNumber"] == 1
    assert second["cursor"] == {"stack": [[{"id": "x"}]]}
    assert "only the first" not in result.text.lower()


@respx.mock
def test_notion_pagination_is_bounded_and_says_so(tmp_path):
    route = respx.post("https://app.notion.com/api/v3/loadPageChunk")
    route.mock(
        return_value=_notion_chunk(
            _notion_block(_NOTION_DASHED, "Root", []), stack=[[{"id": "forever"}]]
        )
    )
    result = NotionFetcher(_settings(tmp_path)).fetch(
        f"https://example.notion.site/Page-{_NOTION_ID}", "cid"
    )
    assert route.call_count == 10
    assert "only the first 10" in result.text.lower()


@respx.mock
def test_notion_later_chunk_failure_keeps_what_was_read_and_says_so(tmp_path):
    route = respx.post("https://app.notion.com/api/v3/loadPageChunk")
    route.side_effect = [
        _notion_chunk(
            {**_notion_block(_NOTION_DASHED, "Root", ["b1"]), **_notion_block("b1", "One")},
            stack=[[{"id": "x"}]],
        ),
        httpx.Response(500),
    ]
    result = NotionFetcher(_settings(tmp_path)).fetch(
        f"https://example.notion.site/Page-{_NOTION_ID}", "cid"
    )
    assert "One" in result.text
    assert "only the first 1" in result.text.lower()


@respx.mock
def test_archive_refusing_the_lookup_says_so_and_keeps_the_page_status(tmp_path):
    respx.get("https://example.com/gone").mock(return_value=httpx.Response(404, text="gone"))
    respx.get("https://archive.org/wayback/available").mock(
        return_value=httpx.Response(429, text="<html>Too Many Requests</html>")
    )

    with pytest.raises(TextFetchError) as caught:
        GenericHtmlFetcher(_settings(tmp_path)).fetch("https://example.com/gone", "cid")

    assert "404" in str(caught.value)
    assert "archive lookup returned HTTP 429" in str(caught.value)
