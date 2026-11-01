import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {createRequire} from "node:module";
import {dirname,join} from "node:path";
import {fileURLToPath} from "node:url";
import test from "node:test";
import ts from "typescript";
const require=createRequire(import.meta.url),root=join(dirname(fileURLToPath(import.meta.url)),"..");
async function load(name){const source=await readFile(join(root,`lib/${name}.ts`),"utf8"),module={exports:{}};
 new Function("module","exports","require",ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText)(module,module.exports,require);return module.exports;}
const tick=()=>new Promise(resolve=>setTimeout(resolve,0));
function storage(){const values=new Map();return {getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,value)};}
function connect(lifecycle,bridge){const target={};const read=()=>{const state=lifecycle.getOpenDraftSaveState();return {...state,generation:1,revision:lifecycle.getDraftMutationEpoch(),ready:true};};
 bridge.installDesktopSaveBridge(target,read,async()=>{let confirmed=true;try{await lifecycle.flushOpenDrafts();}catch{confirmed=false;}
 const state=read();return {serverConfirmed:confirmed&&!state.dirty&&!state.saving&&!state.conflict,localRecoverable:state.localRecoverable,state};});return target;}
function registerNewDraft(lifecycle,draft){const scope="TEST_ONLY-newchat-owner-project";draft.subscribe(lifecycle.markDraftStateChanged);
 lifecycle.registerDraftSaveState(()=>{const state=draft.snapshot();return {dirty:state.dirty,saving:state.pending,conflict:state.uncertain,offline:false,localRecoverable:draft.localRecoverable()};},scope);
 lifecycle.registerDraftFlush(()=>draft.flush(),scope);}

test("unsent new-chat text cannot acknowledge close before real conversation plus draft confirmation",async()=>{
 const lifecycle=await load("draft-lifecycle"),bridge=await load("desktop-save-bridge"),{NewConversationDraft}=await load("new-conversation-draft");
 let finishCreate,finishSave,creates=0,saves=0;const draft=new NewConversationDraft("owner","project",storage(),{
  create:()=>{creates++;return new Promise(resolve=>{finishCreate=resolve;});},
  save:()=>{saves++;return new Promise(resolve=>{finishSave=resolve;});},ready:()=>{},
 });registerNewDraft(lifecycle,draft);const target=connect(lifecycle,bridge);
 target.__KONTUR_DESKTOP_REQUEST_CLOSE__("initial");await tick();assert.equal(target.__KONTUR_DESKTOP_STATE__.close.status,"safe");
 draft.edit("unsent private text");assert.equal(target.__KONTUR_DESKTOP_STATE__.close.status,"blocked");
 target.__KONTUR_DESKTOP_REQUEST_CLOSE__("typed");await tick();assert.equal(creates,1);assert.equal(saves,0);assert.equal(target.__KONTUR_DESKTOP_STATE__.pending,true);
 const same=draft.flush();assert.equal(same,draft.flush());finishCreate("TEST_ONLY-conversation");await tick();assert.equal(saves,1);assert.notEqual(target.__KONTUR_DESKTOP_STATE__.close.status,"safe");
 finishSave(true);await same;await tick();assert.equal(target.__KONTUR_DESKTOP_STATE__.close.status,"safe");
 let whitespaceCreates=0;const whitespace=new NewConversationDraft("owner","another",storage(),{create:async()=>{whitespaceCreates++;return "bad";},save:async()=>true,ready:()=>{}});
 whitespace.edit(" \n ");await whitespace.flush();assert.equal(whitespaceCreates,0);assert.equal(whitespace.snapshot().dirty,false);
});

test("uncertain new conversation creation retains owner/project recovery and never blindly repeats POST",async()=>{
 const lifecycle=await load("draft-lifecycle"),bridge=await load("desktop-save-bridge"),{NewConversationDraft}=await load("new-conversation-draft");
 const local=storage();let creates=0;const dependencies={create:async()=>{creates++;throw new Error("TEST_ONLY response lost");},save:async()=>true,ready:()=>{}};
 const draft=new NewConversationDraft("owner","project",local,dependencies);registerNewDraft(lifecycle,draft);draft.edit("recover me");
 await assert.rejects(draft.flush());assert.equal(draft.snapshot().uncertain,true);assert.equal(draft.localRecoverable(),true);
 const target=connect(lifecycle,bridge);target.__KONTUR_DESKTOP_REQUEST_CLOSE__("uncertain");await tick();assert.equal(creates,1);assert.equal(target.__KONTUR_DESKTOP_STATE__.close.status,"blocked");
 const restored=new NewConversationDraft("owner","project",local,dependencies);assert.equal(restored.snapshot().text,"recover me");await assert.rejects(restored.flush());assert.equal(creates,1);
 const foreign=new NewConversationDraft("foreign","project",local,dependencies);assert.equal(foreign.snapshot().text,"");
 const otherProject=new NewConversationDraft("owner","foreign-project",local,dependencies);assert.equal(otherProject.snapshot().text,"");
});

for(const order of ["reader-first","flusher-first"])test(`detached save remains visible through failure and confirmed same-context return (${order})`,async()=>{
 const lifecycle=await load("draft-lifecycle"),bridge=await load("desktop-save-bridge");let dirty=false,rejectSave,attempts=0;
 const scope="TEST_ONLY-exact-session-code";
 const unstate=lifecycle.registerDraftSaveState(()=>({dirty,saving:false,conflict:false,offline:false,localRecoverable:true}),scope,()=>"exact-unsent-bytes");
 const unflush=lifecycle.registerDraftFlush(()=>{if(!dirty)return Promise.resolve();attempts++;if(attempts===1)return new Promise((_,reject)=>{rejectSave=reject;});return Promise.reject(new Error("stale detached editor"));},scope,()=>"exact-unsent-bytes");
 const target=connect(lifecycle,bridge);target.__KONTUR_DESKTOP_REQUEST_CLOSE__("prior");await tick();assert.equal(target.__KONTUR_DESKTOP_STATE__.close.status,"safe");
 dirty=true;if(order==="reader-first"){unstate();unflush();}else{unflush();unstate();}await tick();
 assert.equal(lifecycle.getOpenDraftSaveState().dirty,true);assert.equal(lifecycle.getOpenDraftSaveState().saving,true);assert.equal(target.__KONTUR_DESKTOP_STATE__.close.status,"blocked");
 target.__KONTUR_DESKTOP_REQUEST_CLOSE__("detached");await tick();assert.equal(attempts,1);rejectSave(new Error("offline"));await tick();
 assert.equal(target.__KONTUR_DESKTOP_STATE__.close.status,"blocked");assert.equal(lifecycle.getOpenDraftSaveState().dirty,true);
 let resumedDirty=true;
 lifecycle.registerDraftSaveState(()=>({dirty:resumedDirty,saving:false,conflict:false,offline:false,localRecoverable:true}),scope,()=>"exact-unsent-bytes");
 lifecycle.registerDraftFlush(async()=>{resumedDirty=false;},scope,()=>"exact-unsent-bytes");
 target.__KONTUR_DESKTOP_REQUEST_CLOSE__("returned");await tick();await tick();
 assert.equal(dirty,true,"old reader intentionally stays stale, so handoff must retire it only after fresh confirmation");
 assert.equal(lifecycle.getOpenDraftSaveState().dirty,false);assert.equal(lifecycle.getOpenDraftSaveState().saving,false);
 assert.equal(target.__KONTUR_DESKTOP_STATE__.close.status,"safe");
});


for(const order of ["reader-first","flusher-first"])test(`different clean server bytes cannot retire an unrecoverable detached conflict (${order})`,async()=>{
 const lifecycle=await load("draft-lifecycle"),bridge=await load("desktop-save-bridge"),drafts=await load("local-drafts");
 const previousWindow=globalThis.window;
 globalThis.window={localStorage:{getItem:()=>null,setItem:()=>{throw new Error("TEST_ONLY quota denied");},removeItem:()=>{}}};
 try{
  const scope="practice:TEST_ONLY-conflicted",kind="code",key=scope+":code",oldText="unsaved private A",serverText="different B";
  drafts.setLocalDraftOwner("A");drafts.saveLocalDraft(scope,kind,1,oldText);
  assert.equal(drafts.readLocalDraft(scope,kind).content,oldText,"failed durable storage retains an in-memory recovery copy");
  assert.equal(drafts.isLocalDraftDurable(scope,kind),false);
  drafts.setLocalDraftOwner("B");assert.equal(drafts.readLocalDraft(scope,kind),null);drafts.setLocalDraftOwner("A");
  const unstate=lifecycle.registerDraftSaveState(()=>({dirty:true,saving:false,conflict:true,offline:false,localRecoverable:false}),key,()=>oldText);
  const unflush=lifecycle.registerDraftFlush(async()=>{throw new Error("TEST_ONLY 409");},key,()=>oldText);
  if(order==="reader-first"){unstate();unflush();}else{unflush();unstate();}await tick();
  let shownText=serverText,dirty=false,conflict=false;
  lifecycle.registerDraftSaveState(()=>({dirty,saving:false,conflict,offline:false,localRecoverable:!dirty}),key,()=>shownText);
  lifecycle.registerDraftFlush(async()=>{},key,()=>shownText);
  const target=connect(lifecycle,bridge);target.__KONTUR_DESKTOP_REQUEST_CLOSE__("different-clean");await tick();await tick();
  assert.equal(target.__KONTUR_DESKTOP_STATE__.close.status,"blocked");assert.equal(lifecycle.getOpenDraftSaveState().conflict,true);
  assert.equal(lifecycle.getOpenDraftSaveState().localRecoverable,false);assert.equal(drafts.readLocalDraft(scope,kind).content,oldText);
  lifecycle.acceptDraftHandoff(key,serverText);assert.equal(lifecycle.getOpenDraftSaveState().dirty,true,"scope and new bytes are not proof of old bytes");
  shownText=drafts.readLocalDraft(scope,kind).content;dirty=true;conflict=true;
  lifecycle.acceptDraftHandoff(key,shownText); // Existing editor explicitly chooses the server copy after comparing the recovered old bytes.
  shownText=serverText;dirty=false;conflict=false;drafts.clearLocalDraft(scope,kind);
  target.__KONTUR_DESKTOP_REQUEST_CLOSE__("explicit-choice");await tick();await tick();
  assert.equal(target.__KONTUR_DESKTOP_STATE__.close.status,"safe");
 }finally{globalThis.window=previousWindow;}
});
