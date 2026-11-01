"use client";

import { useEffect, useMemo, useRef,useState } from "react";
import type { CSSProperties } from "react";

export function CodeEditor({
  value,
  onChange,
  onBlur,
  readOnly = false,
  onSelection,
  draftKey,
  fontSize = 14,
  wrap = false,
  tabSize = 4,
  lineNumbers = true,
}: {
  value: string;
  onChange: (value: string) => void;
  onBlur?: () => void;
  readOnly?: boolean;
  onSelection?:(text:string)=>void;
  draftKey?:string;
  fontSize?: 12 | 14 | 16;
  wrap?: boolean;
  tabSize?: 2 | 4 | 8;
  lineNumbers?: boolean;
}) {
  const [selectedText,setSelectedText]=useState("");
  const numbersRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const highlightRef = useRef<HTMLPreElement>(null);
  const tabEscapeRef = useRef(false);
  const tokens = useMemo(() => highlightPython(value), [value]);
  const lineCount = Math.max(1, value.split("\n").length);

  useEffect(() => {
    const textarea = textareaRef.current;
    const numbers = numbersRef.current;
    if (!textarea || !numbers) return;
    numbers.scrollTop = textarea.scrollTop;
  }, [value]);

  useEffect(()=>{
    if(!draftKey)return;
    try{const saved=JSON.parse(sessionStorage.getItem("kontur:cursor:"+draftKey)||"null");if(saved&&textareaRef.current){textareaRef.current.selectionStart=Math.min(value.length,saved.start??0);textareaRef.current.selectionEnd=Math.min(value.length,saved.end??0);textareaRef.current.scrollTop=saved.scrollTop??0;}}
    catch{}
  },[draftKey]);
  function rememberCursor(){
    const input=textareaRef.current;if(!input)return;
    setSelectedText(value.slice(input.selectionStart,input.selectionEnd));
    if(draftKey)try{sessionStorage.setItem("kontur:cursor:"+draftKey,JSON.stringify({start:input.selectionStart,end:input.selectionEnd,scrollTop:input.scrollTop}));}catch{}
  }
  return (
    <div className={"code-editor" + (readOnly ? " is-readonly" : "") + (wrap ? " is-wrapped" : "")} style={{ "--editor-font-size": `${fontSize}px`, "--editor-tab-size": tabSize } as CSSProperties}>
      <div aria-hidden="true" className="line-numbers" hidden={!lineNumbers} ref={numbersRef}>
        {Array.from({ length: lineCount }, (_, index) => (
          <span key={index}>{index + 1}</span>
        ))}
      </div>
      <div className="code-input-layer">
      <pre className="code-highlight" aria-hidden="true" ref={highlightRef}>{tokens.map((token, index) => <span className={token.kind ? "syntax-" + token.kind : undefined} key={index}>{token.text}</span>)}{"\n"}</pre>
      <textarea
        aria-label="Редактор Python"
        aria-describedby="code-keyboard-help"
        className="code-textarea"
        readOnly={readOnly}
        placeholder="Начните писать код…"
        onSelect={rememberCursor}
        onChange={(event) => onChange(event.target.value)}
        onBlur={onBlur}
        onKeyDown={(event) => {
          if (event.key === "Escape") { tabEscapeRef.current = true; return; }
          if (event.key === "Tab") {
            if (tabEscapeRef.current || event.shiftKey) { tabEscapeRef.current = false; return; }
            event.preventDefault();
            const textarea = event.currentTarget;
            const start = textarea.selectionStart;
            const end = textarea.selectionEnd;
            const indentation = " ".repeat(tabSize);
            const next = value.slice(0, start) + indentation + value.slice(end);
            onChange(next);
            requestAnimationFrame(() => {
              textarea.selectionStart = start + tabSize;
              textarea.selectionEnd = start + tabSize;
            });
          }
        }}
        onScroll={(event) => {
          rememberCursor();
          if (numbersRef.current) numbersRef.current.scrollTop = event.currentTarget.scrollTop;
          if (highlightRef.current) {
            highlightRef.current.scrollTop = event.currentTarget.scrollTop;
            highlightRef.current.scrollLeft = event.currentTarget.scrollLeft;
          }
        }}
        ref={textareaRef}
        spellCheck={false}
        value={value}
      />
      {selectedText&&onSelection&&<button className="outline-button code-selection-action" type="button" onMouseDown={e=>e.preventDefault()} onClick={()=>onSelection(selectedText)}>Спросить о фрагменте</button>}
      <span className="sr-only" id="code-keyboard-help">Tab добавляет отступ. Нажмите Escape, затем Tab, чтобы выйти из редактора.</span>
      </div>
    </div>
  );
}

function highlightPython(code: string): Array<{ text: string; kind?: string }> {
  const pattern = /#[^\n]*|"""[\s\S]*?(?:"""|$)|'''[\s\S]*?(?:'''|$)|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|\b(?:and|as|assert|async|await|break|class|continue|def|del|elif|else|except|finally|for|from|global|if|import|in|is|lambda|nonlocal|not|or|pass|raise|return|try|while|with|yield)\b|\b(?:True|False|None)\b|\b(?:print|range|len|int|str|float|list|dict|set|tuple|open|max|min|sum|sorted|enumerate|zip|map|abs)\b|\b\d+(?:\.\d+)?\b/g;
  const result: Array<{ text: string; kind?: string }> = [];
  let cursor = 0;
  for (const match of code.matchAll(pattern)) {
    const index = match.index ?? 0;
    if (index > cursor) result.push({ text: code.slice(cursor, index) });
    const text = match[0];
    const kind = text.startsWith("#") ? "comment" : /^["']/.test(text) ? "string" : /^\d/.test(text) || /^(True|False|None)$/.test(text) ? "literal" : /^(print|range|len|int|str|float|list|dict|set|tuple|open|max|min|sum|sorted|enumerate|zip|map|abs)$/.test(text) ? "builtin" : "keyword";
    result.push({ text, kind });
    cursor = index + text.length;
  }
  if (cursor < code.length) result.push({ text: code.slice(cursor) });
  return result;
}
