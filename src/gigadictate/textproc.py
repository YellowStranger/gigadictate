"""Turning model output pieces into the final text to insert."""

from __future__ import annotations

import re

_SENT_END = ".!?…"

# "новая строка" / "с новой строки" -> line break, "новый абзац" -> blank line.
# The e2e model capitalises and punctuates, so the command may come as "Новая строка." or ", новая строка,".
_CMD = re.compile(
    r"[ \t]*[,.;:!?…]?[ \t]*\b(?P<cmd>нов(?:ая|ую)\s+строк[аиу]|с\s+новой\s+строки|нов(?:ый|ого)\s+абзац(?:а)?|с\s+нового\s+абзаца)\b[ \t]*[,.;:!?…]?[ \t]*",
    re.IGNORECASE,
)


_MISSING_SPACE = re.compile(r"([,:;!?…])(?=[«A-Za-zА-Яа-яЁё])|(?<=[A-Za-zА-Яа-яЁё])\.(?=[«А-ЯЁ][а-яё])")


def fix_spacing(text: str) -> str:
    """The model sometimes glues punctuation: 'говорит:«После' -> 'говорит: «После'.
    Times like 15:30 and abbreviations like 'т.е.' are left alone."""
    return _MISSING_SPACE.sub(lambda m: (m.group(1) or ".") + " ", text)


def join_segments(parts: list[str]) -> str:
    out = ""
    for p in parts:
        p = fix_spacing(" ".join(p.split()))
        if not p:
            continue
        if out and out[-1] not in _SENT_END and p[:1].isupper() and p[1:2].islower():
            # A cut inside a sentence: the model starts each piece with a capital letter.
            p = p[0].lower() + p[1:]
        out = f"{out} {p}" if out else p
    return out


_SIGNS = {"точка с запятой": ";", "вопросительный знак": "?", "восклицательный знак": "!",
          "запятая": ",", "точка": ".", "двоеточие": ":", "многоточие": "…", "тире": " —"}
_PUNCT_COMMAND = re.compile(
    r"[ ,.;:!?…]*\b(" + "|".join(_SIGNS) + r")\b[ ,.;:!?…]*", re.IGNORECASE
)


def apply_voice_commands(text: str) -> str:
    def repl(m: re.Match) -> str:
        cmd = m.group("cmd").lower()
        nl = "\n\n" if "абзац" in cmd else "\n"
        before = text[: m.start()].rstrip()
        # keep sentence punctuation the user dictated before the command
        punct = m.group(0).strip()[:1]
        keep = punct if punct in _SENT_END and before and before[-1] not in _SENT_END else ""
        return keep + nl

    out = _CMD.sub(repl, text)
    out = _PUNCT_COMMAND.sub(lambda m: _SIGNS[m[1].lower()] + " ", out)
    out = re.sub(r"([.!?…]) ([а-яё])", lambda m: m[1] + " " + m[2].upper(), out)
    # capitalise the first letter of each new line
    out = re.sub(r"(^|\n)([a-zа-яё])", lambda m: m.group(1) + m.group(2).upper(), out)
    return out.strip(" ")


def finalize(parts: list[str], *, voice_commands: bool, trailing_space: bool) -> str:
    text = join_segments(parts)
    if voice_commands:
        text = apply_voice_commands(text)
    text = text.strip(" ")
    if not text.strip():
        return ""
    if trailing_space and not text.endswith(("\n", " ")):
        text += " "
    return text
