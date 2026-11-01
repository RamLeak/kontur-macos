"use client";
import {useEffect,useRef,useState} from "react";
import {DEMO_MODE,request} from "@/lib/api";
import {useShortcuts} from "@/lib/kontur-shortcuts";
import {useOwnerRecord} from "@/lib/owner-sync";
import {SyncConflict} from "./sync-conflict";
import {CorrectAnswerCelebration} from "./correct-answer-celebration";
import {readableError} from "@/lib/ui-copy";
import type {Attempt,TaskRecord} from "@/lib/types";
type CheckResult={attempt_id:string|null;status:"correct"|"incorrect"|"unavailable";correct:boolean|null;reason:string|null};
export function PracticeAnswer({sessionId,task,attempts,sealed,beforeCheck}:{sessionId:string;task:TaskRecord;attempts:Attempt[];sealed:boolean;beforeCheck:()=>Promise<unknown>}){
 const [answer,setAnswer]=useState("");const [result,setResult]=useState<CheckResult|null>(null);
 const [error,setError]=useState<string|null>(null);const [busy,setBusy]=useState(false);
 const [celebrationReceipt,setCelebrationReceipt]=useState<string|null>(null);
 const keyRef=useRef<string|null>(null);const busyRef=useRef(false);
 const sync=useOwnerRecord(!sealed&&!DEMO_MODE?{kind:"practice_answer",solve_session_id:sessionId,
  task_id:task.id,task_revision_id:task.revision.id,content_hash:task.revision.content_hash}:null,{answer:""});
 useEffect(()=>{if(sync.state.ready&&typeof sync.state.value.answer==="string")setAnswer(sync.state.value.answer);},[sync.state.ready,sync.state.value]);
 function edit(value:string){setAnswer(value);sync.edit({answer:value});setResult(null);setError(null);keyRef.current=null;}
 async function check(){
  if(!answer.trim()||sealed||busyRef.current)return;
  busyRef.current=true;setBusy(true);setError(null);
  try{
   await beforeCheck();
   if(DEMO_MODE){setResult({attempt_id:null,status:"unavailable",correct:null,reason:"В демонстрационном наборе ответы не проверяются сервером."});return;}
   keyRef.current??=crypto.randomUUID();
   const response=await request<CheckResult>("/solve-sessions/"+encodeURIComponent(sessionId)+"/check-answer",{method:"POST",headers:{"Idempotency-Key":keyRef.current},body:JSON.stringify({answer})});
   setResult(response);
   if(response.status==="correct"&&response.correct===true&&typeof response.attempt_id==="string"&&response.attempt_id.length>0)setCelebrationReceipt(response.attempt_id);
  }catch(reason){setError(readableError(reason,"Проверка сейчас недоступна. Ответ остался в поле."));}
  finally{busyRef.current=false;setBusy(false);}
 }
 useShortcuts("workspace",{"answer.check":()=>void check()});
 return <section className="kontur-answer" aria-label="Ответ задания">
  <CorrectAnswerCelebration receipt={celebrationReceipt}/>
  <label htmlFor={"answer-"+sessionId}>Ваш ответ</label>
  <input id={"answer-"+sessionId} value={answer} onChange={e=>edit(e.target.value)} disabled={sealed||busy} placeholder="Введите ответ" autoComplete="off" onKeyDown={e=>{if(e.key==="Enter"&&!e.nativeEvent.isComposing)void check();}}/>
  <button className="primary-button" disabled={sealed||busy||!answer.trim()||result?.status==="correct"} onClick={()=>void check()} type="button">{busy?"Проверяем…":"Проверить ответ"}</button>
  <SyncConflict state={sync.state} resolve={sync.resolve}/>
  {result&&<p role="status" className={"answer-result "+result.status}>{result.status==="correct"?"Верно":result.status==="incorrect"?"Ответ неверный. Попробуйте ещё раз.":result.reason||"Для этого задания нет проверяемого эталона."}</p>}
  {error&&<p role="alert" className="answer-result unavailable">{error}</p>}
 </section>;
}
