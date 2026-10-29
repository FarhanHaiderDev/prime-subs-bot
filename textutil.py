"""Text length helpers that count the way Telegram does.

Telegram limits a message to 4096 characters measured in UTF-16 code units, so an emoji outside
the Basic Multilingual Plane counts as 2. Python's len() counts code points, which under-counts
those characters; slicing by code points can therefore still produce a message Telegram rejects.
These helpers measure and cut in UTF-16 units and never split a character.
"""


def _units(char: str) -> int:
    return 2 if ord(char) > 0xFFFF else 1


def utf16_len(text: str) -> int:
    return sum(_units(ch) for ch in text)


def truncate_utf16(text: str, limit: int) -> str:
    """The longest prefix of `text` that is at most `limit` UTF-16 units long."""
    used = 0
    for index, char in enumerate(text):
        used += _units(char)
        if used > limit:
            return text[:index]
    return text


def split_utf16(text: str, limit: int) -> list[str]:
    """Cut `text` into consecutive pieces of at most `limit` UTF-16 units each; joining the
    pieces gives the original text back. Empty text gives an empty list."""
    pieces, start, used = [], 0, 0
    for index, char in enumerate(text):
        width = _units(char)
        if used + width > limit:
            pieces.append(text[start:index])
            start, used = index, 0
        used += width
    if start < len(text):
        pieces.append(text[start:])
    return pieces
