"""Разбор protobuf без схемы (wire format) — чтобы читать кадры sporthub/BetBoom.

decode() возвращает список (номер_поля, значение). LEN-поле пытаемся разобрать
как вложенное сообщение, затем как UTF-8 строку, иначе оставляем bytes.
"""

from __future__ import annotations

import struct


class WireError(ValueError):
    pass


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        if i >= len(buf):
            raise WireError("обрезанный varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7
        if shift > 63:
            raise WireError("слишком длинный varint")


def _parse(buf: bytes) -> list[tuple[int, object]]:
    out: list[tuple[int, object]] = []
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        field, wt = key >> 3, key & 7
        if field == 0:
            raise WireError("поле 0")
        if wt == 0:
            v, i = _varint(buf, i)
        elif wt == 1:
            if i + 8 > len(buf):
                raise WireError("обрезанный fixed64")
            v = struct.unpack("<d", buf[i : i + 8])[0]
            i += 8
        elif wt == 2:
            n, i = _varint(buf, i)
            if i + n > len(buf):
                raise WireError("обрезанное LEN-поле")
            v = _len_value(buf[i : i + n])
            i += n
        elif wt == 5:
            if i + 4 > len(buf):
                raise WireError("обрезанный fixed32")
            v = struct.unpack("<f", buf[i : i + 4])[0]
            i += 4
        else:
            raise WireError(f"неизвестный wire type {wt}")
        out.append((field, v))
    return out


def _len_value(chunk: bytes):
    if not chunk:
        return ""
    try:
        text = chunk.decode("utf-8")
        if text.isprintable():
            return text
    except UnicodeDecodeError:
        pass
    try:
        return _parse(chunk)
    except WireError:
        return chunk


def decode(buf: bytes) -> list[tuple[int, object]]:
    return _parse(buf)


def strings(tree) -> list[str]:
    """Все строки из разобранного дерева (для быстрого просмотра: команды, турниры)."""
    found: list[str] = []
    if isinstance(tree, str):
        found.append(tree)
    elif isinstance(tree, list):
        for _, v in tree:
            found.extend(strings(v))
    return found


def dump(tree, indent: int = 0, max_lines: int = 200) -> str:
    lines: list[str] = []

    def walk(t, depth):
        for field, v in t:
            if len(lines) >= max_lines:
                return
            pad = "  " * depth
            if isinstance(v, list):
                lines.append(f"{pad}{field}:")
                walk(v, depth + 1)
            elif isinstance(v, bytes):
                lines.append(f"{pad}{field}: <{len(v)} bytes>")
            else:
                lines.append(f"{pad}{field}: {v!r}")

    walk(tree, indent)
    return "\n".join(lines)
