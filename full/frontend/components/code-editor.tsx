"use client";

import { useEffect, useMemo, useRef,useState } from "react";
import type { CSSProperties } from "react";
import { applyPythonEdit, type PythonEditResult } from "@/lib/python-editing";
import { findPythonBracketState, highlightPython } from "@/lib/python-highlight";

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
  const [caretPosition,setCaretPosition]=useState(0);
  const numbersRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const highlightRef = useRef<HTMLPreElement>(null);
  const tabEscapeRef = useRef(false);
  const tokens = useMemo(() => highlightPython(value), [value]);
  const bracketState = useMemo(() => findPythonBracketState(value, caretPosition), [caretPosition, value]);
  const lineCount = Math.max(1, value.split("\n").length);

  useEffect(() => {
    const textarea = textareaRef.current;
    const numbers = numbersRef.current;
    if (!textarea || !numbers) return;
    numbers.scrollTop = textarea.scrollTop;
  }, [value]);

  useEffect(()=>{
    if(!draftKey)return;
    try{const saved=JSON.parse(sessionStorage.getItem("kontur:cursor:"+draftKey)||"null");if(saved&&textareaRef.current){const start=Math.min(value.length,saved.start??0);const end=Math.min(value.length,saved.end??0);setCaretPosition(start);textareaRef.current.selectionStart=start;textareaRef.current.selectionEnd=end;textareaRef.current.scrollTop=saved.scrollTop??0;}}
    catch{}
  },[draftKey]);
  function rememberCursor(){
    const input=textareaRef.current;if(!input)return;
    setCaretPosition(input.selectionStart);
    setSelectedText(value.slice(input.selectionStart,input.selectionEnd));
    if(draftKey)try{sessionStorage.setItem("kontur:cursor:"+draftKey,JSON.stringify({start:input.selectionStart,end:input.selectionEnd,scrollTop:input.scrollTop}));}catch{}
  }
  function applyEditorEdit(edit: PythonEditResult) {
    const input = textareaRef.current;
    if (input && edit.value !== value) {
      // WebView2's text insertion keeps the browser undo transaction, unlike
      // replacing the controlled value alone after an indentation edit.
      let start = 0, oldEnd = value.length, newEnd = edit.value.length;
      while (start < oldEnd && start < newEnd && value[start] === edit.value[start]) start += 1;
      while (oldEnd > start && newEnd > start && value[oldEnd - 1] === edit.value[newEnd - 1]) { oldEnd -= 1; newEnd -= 1; }
      input.setSelectionRange(start, oldEnd);
      document.execCommand("insertText", false, edit.value.slice(start, newEnd));
    }
    onChange(edit.value);
    setCaretPosition(edit.selectionStart);
    setSelectedText(edit.value.slice(edit.selectionStart, edit.selectionEnd));
    requestAnimationFrame(() => {
      const textarea = textareaRef.current;
      if (!textarea) return;
      textarea.selectionStart = edit.selectionStart;
      textarea.selectionEnd = edit.selectionEnd;
      if (draftKey) try { sessionStorage.setItem("kontur:cursor:" + draftKey, JSON.stringify({ start: edit.selectionStart, end: edit.selectionEnd, scrollTop: textarea.scrollTop })); } catch {}
    });
  }
  return (
    <div className={"code-editor" + (readOnly ? " is-readonly" : "") + (wrap ? " is-wrapped" : "")} style={{ "--editor-font-size": `${fontSize}px`, "--editor-tab-size": tabSize } as CSSProperties}>
      <div aria-hidden="true" className="line-numbers" hidden={!lineNumbers} ref={numbersRef}>
        {Array.from({ length: lineCount }, (_, index) => (
          <span key={index}>{index + 1}</span>
        ))}
      </div>
      <div className="code-input-layer">
      <pre className="code-highlight" aria-hidden="true" ref={highlightRef}>{tokens.map((token) => {
        const bracketClass = token.kind === "bracket"
          ? bracketState.unmatched === token.start
            ? " syntax-bracket-unmatched"
            : bracketState.active === token.start || bracketState.match === token.start
              ? " syntax-bracket-match"
              : " syntax-bracket"
          : "";
        return <span className={(token.kind ? "syntax-" + token.kind : "") + bracketClass} key={token.start}>{token.text}</span>;
      })}{"\n"}</pre>
      <textarea
        aria-label="Редактор Python"
        aria-describedby="code-keyboard-help"
        className="code-textarea"
        readOnly={readOnly}
        placeholder="Начните писать код…"
        onSelect={rememberCursor}
        onChange={(event) => {
          setCaretPosition(event.currentTarget.selectionStart);
          onChange(event.target.value);
        }}
        onBlur={onBlur}
        onKeyDown={(event) => {
          if (readOnly || event.nativeEvent.isComposing || event.ctrlKey || event.metaKey || event.altKey) return;
          if (event.key === "Escape") { tabEscapeRef.current = true; return; }
          if (event.key === "Tab") {
            const textarea = event.currentTarget;
            if (tabEscapeRef.current && !event.shiftKey) { tabEscapeRef.current = false; return; }
            tabEscapeRef.current = false;
            const edit = applyPythonEdit({ code: value, selectionStart: textarea.selectionStart, selectionEnd: textarea.selectionEnd, key: "Tab", shiftKey: event.shiftKey, tabSize });
            if (edit) { event.preventDefault(); applyEditorEdit(edit); }
            return;
          }
          if (event.key === "Enter" || event.key === "Backspace" || event.key === ")" || event.key === "]" || event.key === "}") {
            const textarea = event.currentTarget;
            const edit = applyPythonEdit({ code: value, selectionStart: textarea.selectionStart, selectionEnd: textarea.selectionEnd, key: event.key, tabSize });
            if (edit) { event.preventDefault(); applyEditorEdit(edit); }
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
