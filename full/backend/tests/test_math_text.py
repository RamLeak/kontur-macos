from __future__ import annotations

from backend.app.math_text import (
    MAX_EXPRESSION_BYTES,
    MAX_EXPRESSION_DEPTH,
    MAX_INPUT_BYTES,
    readable_math_text,
)


def test_inline_and_display_blocks_render_without_touching_surrounding_text() -> None:
    source = r"До \(x^2 + y_1\), затем \[\frac{a+b}{\sqrt{x}}\]."

    assert readable_math_text(source) == "До x² + y₁, затем (a+b)/(√(x))."


def test_common_operators_and_blackboard_symbols() -> None:
    source = r"\(\neg A \lor B \land C \to D \equiv E \geq F \leq G \neq H \in I \notin J \mid K \lt L\)"

    assert readable_math_text(source) == "¬ A ∨ B ∧ C → D ≡ E ≥ F ≤ G ≠ H ∈ I ∉ J | K < L"


def test_text_vectors_roots_limits_and_spacing() -> None:
    source = r"\(\text{if}+\textit{a\%}+\mathbb{R}+\vec{x}+\sqrt[3]{y}+\sum\limits_{i=1}^n\)"

    assert readable_math_text(source) == "if+a%+ℝ+x⃗+√[3](y)+∑ᵢ₌₁ⁿ"


def test_delimiters_dfrac_and_bare_blackboard_symbol() -> None:
    source = r"\(\left\{\dfrac{a}{b}\right\}+\mathbb N\leqslant\mathbb{Z}\)"

    assert readable_math_text(source) == "{(a)/(b)}+ℕ≤ℤ"


def test_braces_and_escaped_special_characters_are_readable() -> None:
    source = r"\(\{x\}+\%\_\{\}+{a+b}^2\)"

    assert readable_math_text(source) == "{x}+%_{}+(a+b)²"


def test_scripts_use_safe_fallback_when_unicode_has_no_character() -> None:
    source = r"\(x^{n+1}+y_{q}\)"

    assert readable_math_text(source) == "xⁿ⁺¹+y_(q)"


def test_unbraced_scripts_consume_one_tex_token_without_losing_following_digits() -> None:
    assert readable_math_text(r"\(x^10+y_12\)") == "x¹0+y₁2"


def test_unknown_commands_and_environments_preserve_the_whole_block() -> None:
    unknown = r"before \(x+\G\) after"
    unknown_upper = r"before \(x+\F\) after"
    environment = r"before \[\begin{cases}x\\y\end{cases}\] after"

    assert readable_math_text(unknown) == unknown
    assert readable_math_text(unknown_upper) == unknown_upper
    assert readable_math_text(environment) == environment


def test_invalid_blocks_are_atomic_and_keep_delimiters() -> None:
    invalid_fraction = r"before \(\frac{x}\) after"
    unclosed_group = r"before \(x^{a\) after"
    empty = r"before \(   \) after"
    unmatched_left = r"before \(\left(x\) after"
    unmatched_right = r"before \(x\right) after"

    assert readable_math_text(invalid_fraction) == invalid_fraction
    assert readable_math_text(unclosed_group) == unclosed_group
    assert readable_math_text(empty) == empty
    assert readable_math_text(unmatched_left) == unmatched_left
    assert readable_math_text(unmatched_right) == unmatched_right


def test_expression_depth_and_size_limits_fall_back_to_original_block() -> None:
    deep_expression = "{" * (MAX_EXPRESSION_DEPTH + 1) + "x" + "}" * (MAX_EXPRESSION_DEPTH + 1)
    deep = rf"\({deep_expression}\)"
    long = rf"\({'x' * (MAX_EXPRESSION_BYTES + 1)}\)"

    assert readable_math_text(deep) == deep
    assert readable_math_text(long) == long


def test_input_limit_and_non_math_content_are_preserved() -> None:
    source = "x" * (MAX_INPUT_BYTES + 1) + r"\(x^2\)"

    assert readable_math_text(source) == source
    assert readable_math_text("plain <b>text</b> \\%") == "plain <b>text</b> \\%"


def test_compound_atom_script_binding_is_explicit() -> None:
    assert readable_math_text(r"\(\frac{a}{b}^2\)") == "((a)/(b))²"
    assert readable_math_text(r"\({a+b}^2\)") == "(a+b)²"
    assert readable_math_text(r"\(\sqrt{x}^2\)") == "(√(x))²"
    assert readable_math_text(r"\(x^{10.5}\)") == "x^(10.5)"
    assert readable_math_text(r"\(x^2^3\)") == r"\(x^2^3\)"


def test_many_small_blocks_and_unterminated_openers_are_bounded() -> None:
    assert readable_math_text(r"\(x\)" * 8000) == "x" * 8000
    source = r"\(" * 10000
    assert readable_math_text(source) == source


def test_text_atom_scripts_keep_the_whole_label_as_base() -> None:
    assert readable_math_text(r"\(\text{a+b}^2\)") == "(a+b)²"


def test_invisible_delimiter_is_consumed_atomically() -> None:
    assert readable_math_text(r"\(\left.x\right|\)") == "x|"
    assert readable_math_text(r"\(\left\{x\right\}\)") == "{x}"
    original = r"\(\left\langle x\right\rangle\)"
    assert readable_math_text(original) == original
