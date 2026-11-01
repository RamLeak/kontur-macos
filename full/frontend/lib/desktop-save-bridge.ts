/** Data-only lifecycle handshake. This module grants no native capabilities. */
export type SaveSnapshot={generation:number;revision?:number;dirty:boolean;saving:boolean;conflict:boolean;offline:boolean;ready:boolean};
type FlushResult={serverConfirmed:boolean;localRecoverable:boolean;state:SaveSnapshot};
export function installDesktopSaveBridge(target:object,read:()=>SaveSnapshot,flush:()=>Promise<FlushResult>,canUpdate?:()=>Promise<boolean>){
 if(Object.getOwnPropertyDescriptor(target,"__KONTUR_DESKTOP_STATE__"))return;
 let close={requestId:"",status:"blocked" as "pending"|"safe"|"blocked"};
 let closeGeneration=read().generation,closeRevision=read().revision??0,updateAllowed=false;
 const clean=(current:SaveSnapshot)=>current.ready&&!current.dirty&&!current.saving&&!current.conflict;
 const state=()=>{
  const current=read();
  if(current.generation!==closeGeneration){close={requestId:"",status:"blocked"};closeGeneration=current.generation;updateAllowed=false;}
  if(close.status==="safe"&&(!clean(current)||(current.revision??0)!==closeRevision)){close={...close,status:"blocked"};updateAllowed=false;}
  return Object.freeze({version:1 as const,generation:current.generation,dirty:current.dirty,
   pending:current.saving||close.status==="pending",serverAcknowledged:clean(current),updateAllowed,
   close:Object.freeze({...close})});
 };
 const requestClose=(requestId:unknown,intent:unknown="close")=>{
  if(typeof requestId!=="string"||! /^[A-Za-z0-9_.:-]{1,128}$/.test(requestId)||(intent!=="close"&&intent!=="update"))return;
  const generation=read().generation;
  closeGeneration=generation;close={requestId,status:"pending"};updateAllowed=false;
  const matches=()=>close.requestId===requestId&&closeGeneration===generation&&read().generation===generation;
  void Promise.resolve().then(flush).then(async result=>{
   let latest=read();if(!matches())return;
   let allowed=result.serverConfirmed&&clean(latest);
   if(intent==="update"){
    const revision=latest.revision??0;
    let authoritative=false;
    if(allowed&&canUpdate){try{authoritative=await canUpdate();}catch{authoritative=false;}}
    latest=read();if(!matches())return;
    allowed=allowed&&authoritative&&clean(latest)&&(latest.revision??0)===revision;
    updateAllowed=allowed;
   }
   closeRevision=latest.revision??0;close={requestId,status:allowed?"safe":"blocked"};
  }).catch(()=>{if(matches()){close={requestId,status:"blocked"};updateAllowed=false;}});
 };
 Object.defineProperties(target,{
  __KONTUR_DESKTOP_STATE__:{get:state,enumerable:false,configurable:false},
  __KONTUR_DESKTOP_REQUEST_CLOSE__:{value:requestClose,writable:false,enumerable:false,configurable:false},
 });
}
