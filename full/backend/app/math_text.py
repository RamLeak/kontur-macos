"""Bounded plain-text presentation of common source formulas, not a TeX engine.

Unknown syntax retains its entire original delimited block. No expression is
evaluated, no HTML is generated and no source command can access resources.
"""
from __future__ import annotations

import re

MAX_INPUT_BYTES = 1_000_000
MAX_EXPRESSION_BYTES = 8_192
MAX_EXPRESSION_DEPTH = 24
_OPEN = re.compile(r"\\[\[(]")
_COMMAND = re.compile(r"[A-Za-z]+")
_SUP = dict(zip("0123456789+-=()nix", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱˣ", strict=True))
_SUB = dict(zip("0123456789+-=()aehipjklmnorstuvx", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢₚⱼₖₗₘₙₒᵣₛₜᵤᵥₓ", strict=True))
_SYMBOLS = {
    "neg": "¬", "lnot": "¬", "to": "→", "rightarrow": "→", "lor": "∨", "vee": "∨",
    "land": "∧", "wedge": "∧", "times": "×", "cdot": "·", "equiv": "≡", "geq": "≥",
    "geqslant": "≥", "leq": "≤", "leqslant": "≤", "in": "∈", "notin": "∉", "neq": "≠",
    "mid": "|", "lt": "<", "gt": ">", "sum": "∑", "forall": "∀", "dots": "…",
}
_SETS = {"N": "ℕ", "Z": "ℤ", "R": "ℝ", "Q": "ℚ", "C": "ℂ"}


def _script(value: str, kind: str) -> str:
    alphabet = _SUP if kind == "^" else _SUB
    if value and all(character in alphabet for character in value):
        return "".join(alphabet[character] for character in value)
    return f"{kind}({value})"


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.left_depth = 0

    def spaces(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def argument(self, depth: int) -> str:
        self.spaces()
        if self.pos >= len(self.text):
            raise ValueError("argument absent")
        if self.text[self.pos] == "{":
            self.pos += 1
            result = self.expression(depth + 1, "}")
        else:
            # TeX permits a single token; its following exponent belongs to
            # the surrounding atom, not to the argument token.
            result, _scope = self.base(depth + 1)
        if not result.strip():
            raise ValueError("empty argument")
        return result

    def base(self, depth: int) -> tuple[str, bool]:
        if depth > MAX_EXPRESSION_DEPTH or self.pos >= len(self.text):
            raise ValueError("formula bound")
        character = self.text[self.pos]
        self.pos += 1
        if character == "{":
            return f"({self.expression(depth + 1, '}')})", False
        if character in "}^_":
            raise ValueError("unbound syntax")
        if character != "\\":
            return character, False
        match = _COMMAND.match(self.text, self.pos)
        if match is None:
            if self.pos >= len(self.text) or self.text[self.pos] not in " ,;!%_{}":
                raise ValueError("unknown escape")
            escaped = self.text[self.pos]
            self.pos += 1
            return (" " if escaped in " ,;!" else escaped), False
        command = match.group()
        self.pos = match.end()
        if command in _SYMBOLS:
            if command == "sum":
                saved = self.pos
                self.spaces()
                if self.text.startswith("\\limits", self.pos):
                    end = self.pos + len("\\limits")
                    if end < len(self.text) and self.text[end].isalpha():
                        raise ValueError("unknown command")
                    self.pos = end
                else:
                    self.pos = saved
            return _SYMBOLS[command], False
        if command in {"left", "right"}:
            self.spaces()
            if self.pos >= len(self.text) or self.text[self.pos] not in "([{}])|.\\":
                raise ValueError("delimiter absent")
            if command == "left":
                self.left_depth += 1
            elif self.left_depth:
                self.left_depth -= 1
            else:
                raise ValueError("unmatched right delimiter")
            delimiter = self.text[self.pos]
            self.pos += 1
            if delimiter == "\\":
                if self.pos >= len(self.text) or self.text[self.pos] not in "{}|":
                    raise ValueError("unsupported named delimiter")
                delimiter = self.text[self.pos]
                self.pos += 1
            return ("" if delimiter == "." else delimiter), False
        if command == "large":
            return "", False
        if command in {"text", "textit"}:
            value = self.argument(depth)
            return value, len(value) > 1
        if command == "vec":
            value = self.argument(depth)
            if len(value) != 1 or not value.isalpha():
                raise ValueError("compound vector unsupported")
            return value + "\u20d7", False
        if command == "mathbb":
            value = self.argument(depth)
            if value not in _SETS:
                raise ValueError("unknown set")
            return _SETS[value], False
        if command in {"frac", "dfrac"}:
            numerator = self.argument(depth)
            denominator = self.argument(depth)
            return f"({numerator})/({denominator})", True
        if command == "sqrt":
            self.spaces()
            index = None
            if self.pos < len(self.text) and self.text[self.pos] == "[":
                self.pos += 1
                index = self.expression(depth + 1, "]")
                if not index.strip():
                    raise ValueError("empty root index")
            value = self.argument(depth)
            if index is None:
                return f"√({value})", True
            return f"√[{index}]({value})", True
        raise ValueError("unsupported command")

    def expression(self, depth: int = 0, stop: str | None = None) -> str:
        if depth > MAX_EXPRESSION_DEPTH:
            raise ValueError("formula nesting bound")
        result: list[str] = []
        while self.pos < len(self.text):
            if stop is not None and self.text[self.pos] == stop:
                self.pos += 1
                return "".join(result)
            atom, scope = self.base(depth)
            kinds: set[str] = set()
            while True:
                saved = self.pos
                self.spaces()
                if self.pos >= len(self.text) or self.text[self.pos] not in "^_":
                    self.pos = saved
                    break
                kind = self.text[self.pos]
                if kind in kinds or not atom.strip():
                    raise ValueError("invalid script")
                kinds.add(kind)
                self.pos += 1
                value = self.argument(depth)
                if scope:
                    atom = f"({atom})"
                    scope = False
                atom += _script(value, kind)
            result.append(atom)
        if stop is not None:
            raise ValueError("unclosed group")
        return "".join(result)


def readable_math_text(text: str) -> str:
    """Keep canonical offsets in the persisted statement, not a DOM overlay."""
    if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        return text
    parts: list[str] = []
    position = 0
    # Each consumed closing delimiter advances position; unmatched input stops
    # scanning. This is linear even for many unterminated opening delimiters.
    for opening in _OPEN.finditer(text):
        if opening.start() < position:
            continue
        closing = "\\)" if opening.group() == "\\(" else "\\]"
        end = text.find(closing, opening.end())
        if end < 0:
            break
        parts.append(text[position:opening.start()])
        body = text[opening.end():end]
        original = text[opening.start():end + 2]
        if not body.strip() or len(body.encode("utf-8")) > MAX_EXPRESSION_BYTES:
            parts.append(original)
        else:
            try:
                parser = _Parser(body)
                rendered = parser.expression()
                if parser.left_depth:
                    raise ValueError("unmatched left delimiter")
                parts.append(rendered)
            except (ValueError, RecursionError):
                parts.append(original)
        position = end + 2
    parts.append(text[position:])
    return "".join(parts)
