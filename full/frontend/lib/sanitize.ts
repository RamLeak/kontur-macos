/**
 * Convert the source statement to plain text before React renders it.
 * The API may retain markup for provenance, but this surface deliberately
 * renders no backend HTML and therefore has no HTML sink to sanitize.
 */
export function statementToText(markup: string): string {
  if (typeof window !== "undefined") {
    const parsed = new DOMParser().parseFromString(markup, "text/html");
    parsed.body.querySelectorAll("script,style,iframe,object,embed").forEach(node=>node.remove());
    parsed.body.querySelectorAll("sup").forEach(node=>node.replaceWith(parsed.createTextNode("^("+node.textContent+")")));
    parsed.body.querySelectorAll("sub").forEach(node=>node.replaceWith(parsed.createTextNode("_("+node.textContent+")")));
    parsed.body.querySelectorAll("td,th").forEach(node=>node.appendChild(parsed.createTextNode("\t")));
    parsed.body.querySelectorAll("br").forEach((node) => node.replaceWith(parsed.createTextNode("\n")));
    parsed.body.querySelectorAll("p,li,tr").forEach((node) => node.appendChild(parsed.createTextNode("\n")));
    return normalize(parsed.body.textContent ?? "");
  }

  // SSR has no standards HTML parser. Keep the value as a React text node
  // until the browser can perform inert DOMParser extraction; never attempt
  // to sanitize arbitrary markup with a regular expression.
  return markup;
}

function normalize(value: string): string {
  return value.replace(/\u00a0/g, " ").replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
}


/** Preserve the API's canonical plain text through the legacy markup adapter.
 * HTML-like comparisons and literal entities must not become tags or change
 * annotation offsets. This is text escaping, never an HTML rendering sink.
 */
export function plainStatementMarkup(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
