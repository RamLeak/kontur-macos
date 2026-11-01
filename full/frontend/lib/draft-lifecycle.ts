export type FlushOptions={pendingOnly?:boolean};
type FlushDraft=(options?:FlushOptions)=>Promise<unknown>;
type ContentKey=()=>string|undefined;
export type DraftSaveState={dirty:boolean;saving:boolean;conflict:boolean;offline:boolean;localRecoverable:boolean};
type DraftRegistration={flush:FlushDraft;scope?:string;content?:ContentKey;detached:boolean;pending:Promise<void>|null;failed:boolean;resolved:boolean};
type StateRegistration={read:()=>DraftSaveState;scope?:string;content?:ContentKey;detached:boolean;resolved:boolean};
const draftFlushers=new Set<DraftRegistration>();
const stateReaders=new Set<StateRegistration>();
let mutationEpoch=0;
const confirmedScopes=new Map<string,Set<string>>();
export function markDraftStateChanged(){mutationEpoch++;}
export function getDraftMutationEpoch(){return mutationEpoch;}
function keyOf(registration:{content?:ContentKey}){try{return registration.content?.();}catch{return undefined;}}
function clean(state:DraftSaveState){return !state.dirty&&!state.saving&&!state.conflict;}

export function registerDraftFlush(flush:FlushDraft,scope?:string,content?:ContentKey):()=>void{
 const registration:DraftRegistration={flush,scope,content,detached:false,pending:null,failed:false,resolved:false};
 draftFlushers.add(registration);markDraftStateChanged();
 return()=>{registration.detached=true;markDraftStateChanged();void flushRegistration(registration).catch(()=>undefined);};
}

function activeContent(scope:string,key:string,requireClean:boolean){
 return [...stateReaders].some(registration=>!registration.detached&&registration.scope===scope&&keyOf(registration)===key&&(!requireClean||clean(registration.read())));
}
function acknowledged(registration:{scope?:string;content?:ContentKey}){
 const key=keyOf(registration);
 return key!==undefined&&Boolean(registration.scope&&confirmedScopes.get(registration.scope)?.has(key)&&activeContent(registration.scope,key,true));
}
function cleanupDetachedReaders(){
 for(const registration of stateReaders){
  if(registration.detached&&(registration.resolved||clean(registration.read())||acknowledged(registration)))stateReaders.delete(registration);
 }
 for(const registration of draftFlushers){
  if(registration.detached&&!registration.pending&&(registration.resolved||acknowledged(registration)))draftFlushers.delete(registration);
 }
 for(const scope of confirmedScopes.keys()){
  if(![...stateReaders].some(registration=>registration.scope===scope)&&![...draftFlushers].some(registration=>registration.scope===scope))confirmedScopes.delete(scope);
 }
}

/** Explicit conflict choice transfers only the exact bytes currently represented by a live editor. */
export function acceptDraftHandoff(scope:string,key:string|undefined){
 if(key===undefined||!activeContent(scope,key,false))return;
 for(const registration of stateReaders){if(registration.detached&&registration.scope===scope&&keyOf(registration)===key)registration.resolved=true;}
 for(const registration of draftFlushers){if(registration.detached&&registration.scope===scope&&keyOf(registration)===key)registration.resolved=true;}
 cleanupDetachedReaders();markDraftStateChanged();
}

function flushRegistration(registration:DraftRegistration,options?:FlushOptions):Promise<void>{
 if(registration.pending)return registration.pending;
 if(registration.resolved)return Promise.resolve();
 registration.failed=false;markDraftStateChanged();
 const operation=Promise.resolve().then(()=>registration.flush(options)).then(()=>{
  registration.failed=false;
  const ownReaders=[...stateReaders].filter(reader=>reader.scope===registration.scope);
  if(registration.detached&&ownReaders.every(reader=>clean(reader.read())))registration.resolved=true;
  if(registration.scope){
   const keys=[...stateReaders].filter(reader=>!reader.detached&&reader.scope===registration.scope&&clean(reader.read())).map(keyOf).filter((key):key is string=>key!==undefined);
   if(keys.length)confirmedScopes.set(registration.scope,new Set(keys));
  }
 }).catch(reason=>{registration.failed=true;throw reason;}).finally(()=>{
  registration.pending=null;cleanupDetachedReaders();markDraftStateChanged();
 });
 registration.pending=operation;return operation;
}

/** Detached editors stay represented until their own ACK, exact-content ACK or explicit matching choice. */
export async function flushOpenDrafts(options?:FlushOptions):Promise<void>{
 const registrations=[...draftFlushers];
 const results=await Promise.allSettled(registrations.map(registration=>flushRegistration(registration,options)));
 cleanupDetachedReaders();
 const failure=results.find((result,index)=>result.status==="rejected"&&draftFlushers.has(registrations[index]));
 if(failure?.status==="rejected")throw failure.reason;
}

export function registerDraftSaveState(read:()=>DraftSaveState,scope?:string,content?:ContentKey){
 if(scope)confirmedScopes.delete(scope);
 const registration:StateRegistration={read,scope,content,detached:false,resolved:false};stateReaders.add(registration);markDraftStateChanged();
 return()=>{registration.detached=true;cleanupDetachedReaders();markDraftStateChanged();};
}
export function getOpenDraftSaveState():DraftSaveState{
 cleanupDetachedReaders();
 const states=[...stateReaders].map(registration=>registration.read());
 const detached=[...draftFlushers].filter(registration=>registration.detached);
 const failed=detached.some(registration=>registration.failed&&!registration.resolved);
 return {dirty:failed||states.some(state=>state.dirty),saving:detached.some(registration=>Boolean(registration.pending))||states.some(state=>state.saving),
  conflict:states.some(state=>state.conflict),offline:states.some(state=>state.offline),
  localRecoverable:!failed&&states.every(state=>!state.dirty||state.localRecoverable)};
}

/** Recovery is owner-scoped; old-account closures cannot block or write a new account. */
export function resetDraftLifecycle(){draftFlushers.clear();stateReaders.clear();confirmedScopes.clear();markDraftStateChanged();}
