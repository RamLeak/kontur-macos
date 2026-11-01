import type {TaskRecord,SolveSession,CodeDocument,BoardDocument,Annotation,TaskCatalogQuery,TaskCatalogPage} from "./types";
import {ApiError} from "./types";
import {DEMO_TASK} from "./demo";
// Explicit TEST_ONLY preview. These are invented training fixtures, never official KEGE records.
function task(id:string,line:number,statement:string,difficulty:"easy"|"medium"|"hard"):TaskRecord{
 return {...DEMO_TASK,id,line_number:line,title:id,difficulty,assets:[],skills:[],exam_year:null,source_year:null,target_exam_year:2027,
  source_url:"demo://fixture/"+id,revision:{...DEMO_TASK.revision,id:"revision-"+id,content_hash:"TEST_ONLY:"+id,
   source_url:"demo://fixture/"+id,statement_html:statement,source_year:null,rights_status:"test_only",target_exam_year:2027}};
}
export const TEST_ONLY_TASKS=[
 task("DEMO-004-001",4,"Для четырёх букв используют двоичный код. Известны кодовые слова: А — 00, Б — 01, В — 10. Найдите кратчайшее слово для Г, чтобы выполнялось условие Фано.","easy"),
 task("DEMO-014-001",14,"Переведите число 110101101 из двоичной системы счисления в десятичную. В ответе укажите число в десятичной системе счисления.","easy"),
 task("DEMO-014-002",14,"Найдите сумму цифр числа 9873. Напишите программу или выполните вычисление на доске. В ответе укажите одно число.","medium"),
 task("DEMO-014-003",14,"Найдите количество единиц в двоичной записи числа 429. В ответе укажите одно целое число.","medium"),
];
const KEY="kontur:test-only:workspaces:v2";
function sessions():SolveSession[]{if(typeof window==="undefined")return [];try{return JSON.parse(localStorage.getItem(KEY)||"[]");}catch{return [];}}
function write(value:SolveSession){if(typeof window==="undefined")return;const all=sessions().filter(s=>s.id!==value.id);all.push(value);localStorage.setItem(KEY,JSON.stringify(all));}
export function findTestTask(id:string){return TEST_ONLY_TASKS.find(t=>t.id===id||t.revision.id===id)??TEST_ONLY_TASKS[1];}
export function testSessions(){return sessions();}
export function newTestSession(revisionId:string,forceNew=false):SolveSession{
 const task=findTestTask(revisionId);const existing=sessions().find(s=>s.task.revision.id===task.revision.id);if(existing&&!forceNew)return existing;
 const id="test-only-"+task.id+(existing&&forceNew?"-"+crypto.randomUUID().slice(0,8):"");const now=new Date().toISOString();
 const value:SolveSession={id,conversation_id:"test-only-chat-"+task.id,task,code:{id:"code-"+id,language:"python",content:"",version:0,updated_at:now},
  board:{id:"board-"+id,version:0,strokes:[],texts:[],viewport:{x:0,y:0,zoom:1},updated_at:now},messages:[],annotations:[],attempts:[],runs:[],sealed_exam:false};
 write(value);return value;
}
export function getTestSession(id?:string){return sessions().find(s=>s.id===id||s.conversation_id===id)??newTestSession("revision-DEMO-014-001");}
export function saveTestCode(id:string,content:string,version:number):CodeDocument{
 const session=getTestSession(id);if(session.code.version!==version)throw new ApiError("VERSION_CONFLICT","Демо-черновик изменился в другой вкладке.",409,{});
 const code={...session.code,content,version:version+1,updated_at:new Date().toISOString()};write({...session,code});return code;
}
export function saveTestBoard(id:string,board:BoardDocument){const session=getTestSession(id);
 if(session.board.version!==board.version)throw new ApiError("VERSION_CONFLICT","Демо-доска изменена в другой вкладке.",409,{});
 const next={...board,version:board.version+1,updated_at:new Date().toISOString()};write({...session,board:next});return next;
}
export function saveTestAnnotation(id:string,annotation:Annotation){const session=getTestSession(id);const existing=session.annotations.find(item=>item.id===annotation.id);if(existing)return existing;write({...session,annotations:[...session.annotations,annotation]});return annotation;}
export function deleteTestAnnotation(id:string,annotationId:string){const session=getTestSession(id);write({...session,annotations:session.annotations.filter(item=>item.id!==annotationId)});}
export function testCatalog(query:TaskCatalogQuery={}):TaskCatalogPage{
 let items=TEST_ONLY_TASKS.filter(t=>(!query.line||t.line_number===query.line)&&(!query.difficulty||t.difficulty===query.difficulty)&&(!query.query||t.revision.statement_html.toLocaleLowerCase("ru").includes(query.query.toLocaleLowerCase("ru")))&&(!query.hasFiles)&&(!query.sourceYear));
 if(query.learningStatus)items=items.filter(t=>testStatus(t.revision.id)===query.learningStatus);
 if(query.bookmarksOnly){let bookmarks:{task_revision_id:string}[]=[];try{bookmarks=JSON.parse(localStorage.getItem("kontur:test-only:bookmarks")||"[]");}catch{};items=items.filter(t=>bookmarks.some(b=>b.task_revision_id===t.revision.id));}
 const offset=Number(query.cursor||0)||0;return {items:items.slice(offset,offset+(query.limit??24)),total:items.length,next_cursor:offset+(query.limit??24)<items.length?String(offset+(query.limit??24)):null};
}
export function testStatus(revisionId:string):"neutral"|"started"{const session=sessions().findLast(s=>s.task.revision.id===revisionId);return session&&(session.code.content.trim()||session.board.strokes.length||session.board.texts.length||session.annotations.length)?"started":"neutral";}
