from __future__ import annotations

from reel_pipeline.text_clean import english_clean


def test_repairs_cp1252_mojibake():
    assert english_clean("real\u00e2\u20ac\u2018time and \u00e2\u20ac\u0153quoted\u00e2\u20ac\x9d caf\u00c3\u00a9") == 'real-time and "quoted" caf\u00e9'


def test_replacement_char_becomes_dash():
    assert english_clean("Report \ufffd 2026-08-13") == "Report - 2026-08-13"
    assert english_clean("JS execution\ufffd which") == "JS execution - which"


def test_odd_hyphens_spaces_and_quotes_normalized():
    raw = "3D\u2011Printed\u00a0desk \u201cnice\u201d it\u2019s\u2026 \u2212 5\u202fkm\u200b"
    assert english_clean(raw) == '3D-Printed desk "nice" it\'s... - 5 km'


def test_emoji_status_words_and_decorative_removed():
    assert english_clean("\u2705 Done \u274c Failed \u26a0\ufe0f Careful \U0001f680 Launch \U0001f447") == (
        "Done Failed Warning: Careful Launch"
    )
    assert english_clean("1\ufe0f\u20e3 First") == "1. First"


def test_language_switcher_lines_dropped():
    text = (
        "intro\n"
        '<a href="docs/README_CN.md"><img alt="\u7b80\u4f53\u4e2d\u6587" src="x"></a>\n'
        "[English](README.md) | [\u65e5\u672c\u8a9e](README_JP.md) | [\ud55c\uad6d\uc5b4](README_KO.md)\n"
        "outro"
    )
    assert english_clean(text) == "intro\noutro"


def test_inline_foreign_script_removed():
    assert english_clean("Supports \u4e2d\u6587 and English") == "Supports and English"


def test_flipped_text_unflipped():
    assert english_clean("> .\u01ddm\u0250u s\u0131\u0265") == "> his name."


def test_keeps_english_typography_and_accents():
    text = "Te P\u0101 T\u016b \u2014 M\u0101ori caf\u00e9 \u2192 next \u2265 5\u00b0 \u20ac10 \u251c\u2500\u2500 src"
    assert english_clean(text) == text


def test_idempotent():
    once = english_clean("real\u00e2\u20ac\u2018time \u2705 \u4e2d\u6587 ok")
    assert english_clean(once) == once


def test_crlf_line_endings_kept():
    assert english_clean("a \u2705\r\nb\r\n") == "a Yes\r\nb\r\n"


def test_frontmatter_never_made_unparseable():
    import yaml

    rsq = chr(0x2019)
    text = f"---\ntitle: 'SEO Isn{rsq}t Dead'\n---\nIt{rsq}s fine\n"
    out = english_clean(text)
    yaml.safe_load(out.split("---\n")[1])
    assert out.endswith("It's fine\n")


def test_status_mark_alone_becomes_word():
    assert english_clean("| a | ✅ | ❌ |") == "| a | Yes | No |"


def test_aligned_tree_spacing_kept():
    line = "├── reddit.py       ▸ OpenCLI"
    assert english_clean(line) == "├── reddit.py       " + chr(0x2192) + " OpenCLI"


def test_fake_fonts_ligatures_and_circled_digits_plain():
    assert english_clean("𝗔𝗕𝗖 ﬁle ①②") == "ABC file 12"
