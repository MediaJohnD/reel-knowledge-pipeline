"""Keep vault text in plain English (John, 2026-11-24 and 2026-11-26).

Model output and scraped captions bring in characters that render as noise in
Obsidian: cp1252 mojibake (``\u00e2\u20ac\u2018``), U+FFFD replacement marks, non-breaking
hyphens and spaces, emoji, upside-down "flipped" text, and language-switcher
badges copied from GitHub READMEs (Chinese, Japanese, Korean, Russian, Arabic,
Hindi...). ``english_clean`` repairs or removes those and leaves ordinary
English typography alone: em dashes, arrows, box-drawing trees, math and
currency signs, and accented Latin letters in place names (M\u0101ori, caf\u00e9).

The function is idempotent, so it is safe to run on every write and over the
whole vault.
"""

from __future__ import annotations

import re
import unicodedata

import yaml

# --- 1. cp1252 mojibake -------------------------------------------------------
_CP1252_HIGH = (
    "\u0080-\u00bf\u0152\u0153\u0160\u0161\u0178\u017d\u017e\u0192\u02c6\u02dc"
    "\u2013\u2014\u2018-\u201e\u2020-\u2022\u2026\u2030\u2039\u203a\u20ac\u2122"
)
_MOJIBAKE = re.compile(f"[\u00c2-\u00f4][{_CP1252_HIGH}]+")


def _to_bytes(run: str) -> bytes | None:
    out = bytearray()
    for ch in run:
        if ord(ch) < 256:
            out.append(ord(ch))
        else:
            try:
                out += ch.encode("cp1252")
            except UnicodeEncodeError:
                return None
    return bytes(out)


def _fix_mojibake_run(match: re.Match[str]) -> str:
    run = match.group(0)
    raw = _to_bytes(run)
    if raw is None:
        return run
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return run


# --- 2. punctuation and invisible characters ---------------------------------
_CHAR_MAP = {
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2212": "-",
    "\u00ad": "",
    "\u00a0": " ",
    "\u202f": " ",
    "\u2007": " ",
    "\u2009": " ",
    "\u200a": " ",
    "\u2002": " ",
    "\u2003": " ",
    "\u205f": " ",
    "\u3000": " ",
    "\u3164": " ",
    "\u2028": "\n",
    "\u2029": "\n",
    "\u200b": "",
    "\u200c": "",
    "\u200d": "",
    "\u2060": "",
    "\ufeff": "",
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201b": "'",
    "\u2032": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u201f": '"',
    "\u2033": '"',
    "\u2026": "...",
}
_CHAR_TABLE = str.maketrans(_CHAR_MAP)
# Fake "bold/italic" caption fonts (Mathematical Alphanumerics), ligatures,
# circled digits/letters, double exclamation and the info sign -> plain text.
_COMPAT = re.compile(
    "["
    + chr(0x1D400)
    + "-"
    + chr(0x1D7FF)
    + chr(0xFB00)
    + "-"
    + chr(0xFB06)
    + chr(0x2460)
    + "-"
    + chr(0x24FF)
    + chr(0x203C)
    + chr(0x2139)
    + "]"
)
_CHAR_TABLE_NO_QUOTES = str.maketrans({k: v for k, v in _CHAR_MAP.items() if v not in ("'", '"')})

# --- 3. emoji ----------------------------------------------------------------
_EMOJI_WORDS = {
    "\u2705": "Yes",
    "\u2714": "Yes",
    "\u2713": "Yes",
    "\u2611": "Yes",
    "\u274c": "No",
    "\u2716": "No",
    "\u2717": "No",
    "\u2718": "No",
    "\u274e": "No",
    "\u26a0": "Warning:",
    "\u26d4": "Blocked:",
    "\U0001f6ab": "Blocked:",
    "\u2610": "[ ]",
    "\u2b50": "*",
    "\u2605": "*",
    "\u2606": "*",
    "\U0001f31f": "*",
    "\U0001f7e2": "Green",
    "\U0001f7e1": "Yellow",
    "\U0001f534": "Red",
    "\U0001f7e0": "Orange",
    "\u26aa": "White",
    "\u26ab": "Black",
    "\u27a1": "\u2192",
    "\u2794": "\u2192",
    "\u279c": "\u2192",
    "\u27a4": "\u2192",
    "\u2b05": "\u2190",
    "\u2b06": "\u2191",
    "\u2b07": "\u2193",
    "\u25b6": "\u2192",
    "\u25ba": "\u2192",
    "\u25b8": "\u2192",
}
_EMOJI_CLASS = (
    "\U0001f000-\U0001faff\u2600-\u27bf\u2b00-\u2bff\u2300-\u23ff"
    "\u25a0-\u25ff\ufe0f\ufe0e\U000e0020-\U000e007f"
)
_KEYCAP = re.compile("([0-9#*])\ufe0f?\u20e3")
_EMOJI_RUN = re.compile(f"[{_EMOJI_CLASS}]+[ \t]?")
# Box-drawing (2500-257F) and arrows (2190-21FF) sit outside these ranges on purpose.
# Geometric shapes used as plain bullets or in trees stay.
_KEEP_SHAPES = set("\u25cf\u25cb\u25a0\u25a1\u25aa\u25ab\u25c6\u25c7")

# --- 4. non-Latin scripts ----------------------------------------------------
_FOREIGN_CLASS = (
    "\u0400-\u052f\u0590-\u05ff\u0600-\u06ff\u0750-\u077f\u0900-\u0dff"
    "\u0e00-\u0e7f\u1100-\u11ff\u3000-\u303f\u3040-\u30ff\u3100-\u31ff"
    "\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff00-\uffef"
)
_FOREIGN = re.compile(f"[{_FOREIGN_CLASS}]")
_FOREIGN_RUN = re.compile(f"[ \t]*[{_FOREIGN_CLASS}][{_FOREIGN_CLASS}\\s]*")
_SWITCHER_HINT = re.compile(r"README|lang|badge|\]\(|<a\s|<img|<p\b|</?div", re.I)

# --- 5. upside-down text -----------------------------------------------------
_FLIP_MARKERS = set(
    "\u0250\u01dd\u0279\u0287\u0265\u026f\u028d\u028e\u025f\u0183\u0254\u0131\u029e\u0259"
)
_UNFLIP = str.maketrans(
    {
        "\u0250": "a",
        "q": "b",
        "\u0254": "c",
        "p": "d",
        "\u01dd": "e",
        "\u0259": "e",
        "\u025f": "f",
        "\u0183": "g",
        "\u0265": "h",
        "\u0131": "i",
        "\u027e": "j",
        "\u029e": "k",
        "\u026f": "m",
        "u": "n",
        "d": "p",
        "b": "q",
        "\u0279": "r",
        "\u0287": "t",
        "n": "u",
        "\u028c": "v",
        "\u028d": "w",
        "\u028e": "y",
        "\u02d9": ".",
        "\u00bf": "?",
        "\u00a1": "!",
        "(": ")",
        ")": "(",
        "[": "]",
        "]": "[",
        "{": "}",
        "}": "{",
        "<": ">",
        ">": "<",
        "'": ",",
        ",": "'",
        "\u2200": "A",
        "\u018e": "E",
        "\u2132": "F",
        "\u05e4": "P",
        "\u22a5": "T",
        "\u2229": "U",
        "\u039b": "V",
        "\u2144": "Y",
    }
)
_LINE_PREFIX = re.compile(r"^(\s*(?:>\s*)*(?:[-*+]\s+|\d+\.\s+)?)(.*)$")


def _unflip_line(line: str) -> str:
    letters = [c for c in line if c.isalpha()]
    markers = sum(1 for c in line if c in _FLIP_MARKERS)
    if markers < 3 or not letters or markers / len(letters) < 0.2:
        return line
    m = _LINE_PREFIX.match(line)
    if m is None:
        return line
    prefix, body = m.groups()
    return prefix + body[::-1].translate(_UNFLIP).strip()


# A status mark followed by a word ("[ok] Ready") adds nothing once it is gone;
# standing alone (a table cell) it becomes the word.
_DROP_BEFORE_TEXT = {"Yes", "No", "Green", "Yellow", "Red", "Orange", "White", "Black"}


def _emoji_sub(match: re.Match[str]) -> str:
    run = match.group(0)
    rest = match.string[match.end() :]
    before_text = bool(rest) and (rest[0].isalnum() or rest[0] in "`*_[(\"'")
    words, kept = [], []
    for ch in run.rstrip(" \t"):
        if ch in _EMOJI_WORDS:
            word = _EMOJI_WORDS[ch]
            if not (before_text and word in _DROP_BEFORE_TEXT):
                words.append(word)
        elif ch in _KEEP_SHAPES:
            kept.append(ch)
    out = " ".join(words) + "".join(kept)
    if out and run[-1] in " \t":
        out += " "
    return out


def _clean_line(line: str) -> str:
    original = line
    line = _unflip_line(line)
    if _FOREIGN.search(line):
        if _SWITCHER_HINT.search(line):
            return "\x00DROP"
        line = _FOREIGN_RUN.sub(" ", line)
    line = _KEYCAP.sub(r"\1.", line)
    line = _EMOJI_RUN.sub(_emoji_sub, line)
    if line != original:
        # Spacing is left alone so aligned trees and tables keep their columns;
        # only whitespace a removed character left at the end of the line goes.
        if original == original.rstrip(" \t"):
            line = line.rstrip(" \t")
        if not line.strip() and original.strip():
            return "\x00DROP"
    return line


def _clean_body(text: str, quotes: bool = True) -> str:
    """Return ``text`` with mojibake repaired and non-English noise removed."""
    if not text:
        return text
    crlf = "\r\n" in text
    if crlf:
        text = text.replace("\r\n", "\n")
    text = text.replace("\u00e2\u20ac\ufffd", '"')
    text = _MOJIBAKE.sub(_fix_mojibake_run, text)
    text = re.sub(r"[ \t]*\ufffd[ \t]*", " - ", text)
    text = text.translate(_CHAR_TABLE if quotes else _CHAR_TABLE_NO_QUOTES)
    text = _COMPAT.sub(lambda m: unicodedata.normalize("NFKC", m.group(0)), text)
    lines = [_clean_line(line) for line in text.split("\n")]
    text = "\n".join(line for line in lines if line != "\x00DROP")
    return text.replace("\n", "\r\n") if crlf else text


def _yaml_ok(block: str) -> bool:
    try:
        yaml.safe_load(block)
        return True
    except yaml.YAMLError:
        return False


def english_clean(text: str) -> str:
    """Return ``text`` with mojibake repaired and non-English noise removed.

    YAML frontmatter is cleaned too, but never left unparseable: a straight
    quote can end a quoted YAML value early, so if the cleaned block no longer
    parses the quotes are kept curly, and if it still fails the block is kept.
    """
    if not text:
        return text
    crlf = "\r\n" in text
    body = text.replace("\r\n", "\n") if crlf else text
    head = ""
    if body.startswith("---\n"):
        end = body.find("\n---", 4)
        if end > 0:
            head, body = body[: end + 1], body[end + 1 :]
    if head:
        inner = head[4:]
        cleaned = _clean_body(inner)
        if not _yaml_ok(cleaned):
            cleaned = _clean_body(inner, quotes=False)
            if not _yaml_ok(cleaned) and _yaml_ok(inner):
                cleaned = inner
        head = "---\n" + cleaned
    out = head + _clean_body(body)
    return out.replace("\n", "\r\n") if crlf else out
