"use client";
import type {SyncState} from "@/lib/owner-sync-store";

const labels:Record<string,string>={theme:"Оформление",reducedMotion:"Меньше анимации",textSize:"Размер текста",defaultTaskType:"Номер задания",
 restoreWorkspace:"Возвращаться к работе",editorTheme:"Оформление редактора",editorFontSize:"Размер шрифта редактора",editorWrap:"Перенос строк",
 editorTabSize:"Размер отступа",editorLineNumbers:"Номера строк",boardBackground:"Фон доски",taskType:"Номер задания",unsolvedOnly:"Только нерешённые"};
const choices:Record<string,Record<string,string>>={theme:{system:"Как в системе",light:"Светлое",dark:"Тёмное"},textSize:{standard:"Обычный",large:"Крупный"},
 editorTheme:{interface:"Как в интерфейсе",light:"Светлое",charcoal:"Тёмное"},boardBackground:{blank:"Чистый лист",grid:"Клетка",dots:"Точки"}};
function present(value:Record<string,unknown>|null|undefined){
 if(!value)return "Пока ничего не сохранено";
 if(typeof value.answer==="string")return value.answer||"Пустой ответ";
 if(typeof value.text==="string")return value.text||"Пустое сообщение";
 const rows=Object.entries(labels).flatMap(([key,label])=>{
  const item=value[key];if(item===undefined)return [];
  const shown=typeof item==="boolean"?(item?"Да":"Нет"):typeof item==="number"?String(item):typeof item==="string"?choices[key]?.[item]:undefined;
  return shown===undefined?[]:[`${label}: ${shown}`];
 });return rows.join("\n")||"Нет сохранённых настроек";
}
export function SyncConflict({state, resolve}: {state: SyncState; resolve: (choice: "local" | "remote") => void}) {
 if(state.conflict)return <aside className="session-notice" role="alert" aria-label="Конфликт черновика">
  <p>На другом устройстве сохранена другая версия. Сравните их и выберите, какую продолжить.</p>
  <details><summary>Сравнить версии</summary>
   <p>На этом устройстве</p><pre style={{whiteSpace:"pre-wrap",maxHeight:160,overflow:"auto"}}>{present(state.value)}</pre>
   <p>На сервере</p><pre style={{whiteSpace:"pre-wrap",maxHeight:160,overflow:"auto"}}>{present(state.remote?.value)}</pre>
  </details>
  <button type="button" onClick={()=>resolve("local")}>Сохранить эту версию</button>
  <button type="button" onClick={()=>resolve("remote")}>Использовать серверную</button>
 </aside>;
 if(state.error&&(state.dirty||state.unsupported))return <p role="status" className="answer-result unavailable">{state.unsupported?
  "Синхронизация пока недоступна на сервере. Продолжайте в этом окне и повторите сохранение позже.":
  "Не удалось подтвердить сохранение. Проверьте подключение и повторите попытку."}</p>;
 return null;
}
