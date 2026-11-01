export type PythonHighlightToken = {
  text: string;
  kind?: string;
  start: number;
};

export type PythonBracketState = {
  active: number | null;
  match: number | null;
  unmatched: number | null;
};

const OPEN_TO_CLOSE: Record<string, string> = { "(": ")", "[": "]", "{": "}" };
const CLOSE_TO_OPEN: Record<string, string> = { ")": "(", "]": "[", "}": "{" };
const BRACKET_CHARS = new Set(Object.keys(OPEN_TO_CLOSE).concat(Object.keys(CLOSE_TO_OPEN)));
const PYTHON_PATTERN = /#[^\n]*|"""[\s\S]*?(?:"""|$)|'''[\s\S]*?(?:'''|$)|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|[()[\]{}]|\b(?:and|as|assert|async|await|break|class|continue|def|del|elif|else|except|finally|for|from|global|if|import|in|is|lambda|nonlocal|not|or|pass|raise|return|try|while|with|yield)\b|\b(?:True|False|None)\b|\b(?:print|range|len|int|str|float|list|dict|set|tuple|open|max|min|sum|sorted|enumerate|zip|map|abs)\b|\b\d+(?:\.\d+)?\b/g;

const KEYWORDS = /^(?:and|as|assert|async|await|break|class|continue|def|del|elif|else|except|finally|for|from|global|if|import|in|is|lambda|nonlocal|not|or|pass|raise|return|try|while|with|yield)$/;
const LITERALS = /^(?:True|False|None)$/;
const BUILTINS = /^(?:print|range|len|int|str|float|list|dict|set|tuple|open|max|min|sum|sorted|enumerate|zip|map|abs)$/;

function scanCodeBrackets(code: string): Array<{ index: number; char: string }> {
  const brackets: Array<{ index: number; char: string }> = [];
  const stackQuote: string[] = [];
  let inComment = false;

  for (let index = 0; index < code.length; index += 1) {
    const char = code[index];
    const quote = stackQuote.at(-1);
    if (inComment) {
      if (char === "\n") inComment = false;
      continue;
    }
    if (quote) {
      if (quote.length === 3) {
        if (code.slice(index, index + 3) === quote) {
          stackQuote.pop();
          index += 2;
        }
      } else if (char === "\\") {
        index += 1;
      } else if (char === quote) {
        stackQuote.pop();
      }
      continue;
    }
    if (char === "#") {
      inComment = true;
    } else if (char === "'" || char === '"') {
      const triple = char.repeat(3);
      if (code.slice(index, index + 3) === triple) {
        stackQuote.push(triple);
        index += 2;
      } else {
        stackQuote.push(char);
      }
    } else if (BRACKET_CHARS.has(char)) {
      brackets.push({ index, char });
    }
  }
  return brackets;
}

export function findPythonBracketState(code: string, caret: number): PythonBracketState {
  const brackets = scanCodeBrackets(code);
  const pairs = new Map<number, number>();
  const unmatched = new Set<number>();
  const stack: Array<{ index: number; char: string }> = [];

  for (const bracket of brackets) {
    if (OPEN_TO_CLOSE[bracket.char]) {
      stack.push(bracket);
      continue;
    }
    const previous = stack.at(-1);
    if (previous && previous.char === CLOSE_TO_OPEN[bracket.char]) {
      stack.pop();
      pairs.set(previous.index, bracket.index);
      pairs.set(bracket.index, previous.index);
    } else {
      unmatched.add(bracket.index);
    }
  }
  stack.forEach((bracket) => unmatched.add(bracket.index));

  const safeCaret = Math.max(0, Math.min(code.length, caret));
  const candidate = [safeCaret - 1, safeCaret].find((index) => brackets.some((item) => item.index === index));
  if (candidate === undefined) return { active: null, match: null, unmatched: null };
  if (unmatched.has(candidate)) return { active: null, match: null, unmatched: candidate };
  const match = pairs.get(candidate);
  return match === undefined
    ? { active: null, match: null, unmatched: candidate }
    : { active: candidate, match, unmatched: null };
}

export function highlightPython(code: string): PythonHighlightToken[] {
  const result: PythonHighlightToken[] = [];
  let cursor = 0;
  for (const match of code.matchAll(PYTHON_PATTERN)) {
    const start = match.index ?? 0;
    if (start > cursor) result.push({ text: code.slice(cursor, start), start: cursor });
    const text = match[0];
    const kind = text.startsWith("#")
      ? "comment"
      : /^['"]/.test(text)
        ? "string"
        : BRACKET_CHARS.has(text)
          ? "bracket"
          : /^\d/.test(text) || LITERALS.test(text)
            ? "literal"
            : BUILTINS.test(text)
              ? "builtin"
              : KEYWORDS.test(text)
                ? "keyword"
                : undefined;
    result.push({ text, kind, start });
    cursor = start + text.length;
  }
  if (cursor < code.length) result.push({ text: code.slice(cursor), start: cursor });
  return result;
}
