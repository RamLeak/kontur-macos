import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {createRequire} from "node:module";
import {dirname,join} from "node:path";
import {fileURLToPath} from "node:url";
import test from "node:test";
import ts from "typescript";
const require=createRequire(import.meta.url),root=join(dirname(fileURLToPath(import.meta.url)),"..");
const source=await readFile(join(root,"lib/desktop-save-bridge.ts"),"utf8");
const module={exports:{}};new Function("module","exports","require",ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText)(module,module.exports,require);
const {installDesktopSaveBridge}=module.exports;
const tick=()=>new Promise(resolve=>setTimeout(resolve,0));
const clean=()=>({generation:1,dirty:false,saving:false,conflict:false,offline:false,ready:true});
test("native close adapter is read-only and local recovery never substitutes server acknowledgement",async()=>{
 const target={};let state={...clean(),dirty:true};let result={serverConfirmed:false,localRecoverable:true};
 installDesktopSaveBridge(target,()=>state,async()=>({...result,state}));
 target.__KONTUR_DESKTOP_REQUEST_CLOSE__("close-1");assert.equal(target.__KONTUR_DESKTOP_STATE__.close.status,"pending");
 await tick();assert.equal(target.__KONTUR_DESKTOP_STATE__.close.status,"blocked");
 state=clean();result={serverConfirmed:true,localRecoverable:true};target.__KONTUR_DESKTOP_REQUEST_CLOSE__("close-2");await tick();
 assert.equal(target.__KONTUR_DESKTOP_STATE__.close.requestId,"close-2");assert.equal(target.__KONTUR_DESKTOP_STATE__.close.status,"safe");
 state={...state,dirty:true};assert.equal(target.__KONTUR_DESKTOP_STATE__.close.status,"blocked");
 assert.equal(Object.isFrozen(target.__KONTUR_DESKTOP_STATE__),true);assert.equal(Object.isFrozen(target.__KONTUR_DESKTOP_STATE__.close),true);
 assert.throws(()=>{target.__KONTUR_DESKTOP_STATE__.dirty=true;},TypeError);
 assert.throws(()=>{target.__KONTUR_DESKTOP_REQUEST_CLOSE__=()=>{};},TypeError);
});
test("late close promises cannot acknowledge a newer request or owner generation",async()=>{
 const target={};let state=clean();const completions=[];
 installDesktopSaveBridge(target,()=>state,()=>new Promise(resolve=>completions.push(resolve)));
 target.__KONTUR_DESKTOP_REQUEST_CLOSE__("A");await tick();target.__KONTUR_DESKTOP_REQUEST_CLOSE__("B");await tick();
 completions[0]({serverConfirmed:true,localRecoverable:true,state});await tick();assert.equal(target.__KONTUR_DESKTOP_STATE__.close.status,"pending");
 state={...state,generation:2};completions[1]({serverConfirmed:true,localRecoverable:true,state});await tick();
 assert.equal(target.__KONTUR_DESKTOP_STATE__.close.status,"blocked");assert.equal(target.__KONTUR_DESKTOP_STATE__.close.requestId,"");
 const before=completions.length;target.__KONTUR_DESKTOP_REQUEST_CLOSE__("bad id");target.__KONTUR_DESKTOP_REQUEST_CLOSE__("x".repeat(129));await tick();
 assert.equal(completions.length,before);
});


test("update intent is default-deny and requires fresh authoritative permission after server confirmation",async()=>{
 const target={};let state=clean(),calls=0,permit=false;
 installDesktopSaveBridge(target,()=>state,async()=>({serverConfirmed:true,localRecoverable:true,state}),async()=>{calls++;return permit;});
 target.__KONTUR_DESKTOP_REQUEST_CLOSE__("ordinary");await tick();assert.equal(calls,0);assert.equal(target.__KONTUR_DESKTOP_STATE__.close.status,"safe");assert.equal(target.__KONTUR_DESKTOP_STATE__.updateAllowed,false);
 target.__KONTUR_DESKTOP_REQUEST_CLOSE__("active-exam","update");await tick();assert.equal(calls,1);assert.equal(target.__KONTUR_DESKTOP_STATE__.close.status,"blocked");assert.equal(target.__KONTUR_DESKTOP_STATE__.updateAllowed,false);
 permit=true;target.__KONTUR_DESKTOP_REQUEST_CLOSE__("no-exam","update");await tick();assert.equal(target.__KONTUR_DESKTOP_STATE__.close.status,"safe");assert.equal(target.__KONTUR_DESKTOP_STATE__.updateAllowed,true);
 state={...state,dirty:true};assert.equal(target.__KONTUR_DESKTOP_STATE__.updateAllowed,false);assert.equal(target.__KONTUR_DESKTOP_STATE__.close.status,"blocked");
 const missing={};installDesktopSaveBridge(missing,clean,async()=>({serverConfirmed:true,localRecoverable:true,state:clean()}));missing.__KONTUR_DESKTOP_REQUEST_CLOSE__("missing","update");await tick();assert.equal(missing.__KONTUR_DESKTOP_STATE__.updateAllowed,false);
});
test("late exam check cannot approve update after generation or registry mutation",async()=>{
 const target={};let state={...clean(),revision:1},finish;
 installDesktopSaveBridge(target,()=>state,async()=>({serverConfirmed:true,localRecoverable:true,state}),()=>new Promise(resolve=>{finish=resolve;}));
 target.__KONTUR_DESKTOP_REQUEST_CLOSE__("check","update");await tick();state={...state,revision:2};finish(true);await tick();assert.equal(target.__KONTUR_DESKTOP_STATE__.updateAllowed,false);assert.equal(target.__KONTUR_DESKTOP_STATE__.close.status,"blocked");
 target.__KONTUR_DESKTOP_REQUEST_CLOSE__("owner-check","update");await tick();state={...state,generation:2};finish(true);await tick();assert.equal(target.__KONTUR_DESKTOP_STATE__.updateAllowed,false);assert.equal(target.__KONTUR_DESKTOP_STATE__.close.status,"blocked");
});
