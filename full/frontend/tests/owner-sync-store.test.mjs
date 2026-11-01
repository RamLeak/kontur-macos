import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {createRequire} from "node:module";
import {dirname,join} from "node:path";
import {fileURLToPath} from "node:url";
import test from "node:test";
import ts from "typescript";
const require=createRequire(import.meta.url);
const root=join(dirname(fileURLToPath(import.meta.url)),"..");
const source=await readFile(join(root,"lib/owner-sync-store.ts"),"utf8");
const output=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText;
const module={exports:{}};new Function("module","exports","require",output)(module,module.exports,require);
const {OwnerSyncStore}=module.exports;
const context={kind:"composer",conversation_id:"TEST_ONLY-conversation"};
function storage(){const map=new Map();return {map,getItem:key=>map.get(key)??null,setItem:(key,value)=>map.set(key,value),removeItem:key=>map.delete(key)};}
function server(){
 let version=0,value=null;const retries=new Map();let loseResponse=false;
 const transport=async(path,init)=>{
  if(!init)return {context,version,value,updated_at:null};
  const input=JSON.parse(init.body);const previous=retries.get(input.idempotency_key);
  if(previous)return previous;
  if(input.base_version!==version)throw {status:409,code:"VERSION_CONFLICT",details:{current:{context,version,value,updated_at:null}}};
  value=input.value;version++;const response={context,version,value,updated_at:null};retries.set(input.idempotency_key,response);
  if(loseResponse){loseResponse=false;throw {status:0,code:"API_UNREACHABLE"};}return response;
 };return {transport,loseNextResponse:()=>{loseResponse=true;},get version(){return version;}};
}
test("two isolated stores preserve both dirty versions and require explicit conflict choice",async()=>{
 const api=server(),a=new OwnerSyncStore("owner",context,{text:""},api.transport,storage()),savedB=storage();
 const b=new OwnerSyncStore("owner",context,{text:""},api.transport,savedB);
 await Promise.all([a.refresh(),b.refresh()]);a.edit({text:"A"});b.edit({text:"B"});await a.flush();
 await assert.rejects(b.flush());assert.equal(b.snapshot().value.text,"B");assert.equal(b.snapshot().remote.value.text,"A");
 assert.equal(b.snapshot().conflict,true);b.resolve("remote");assert.equal(b.snapshot().value.text,"A");
 assert.match([...savedB.map.values()].find(value=>value.includes('"local"')),/"text":"B"/);
 b.edit({text:"resolved B"});await b.flush();await a.refresh();assert.equal(a.snapshot().value.text,"resolved B");
});
test("uncertain save recovery retries same durable request after restart",async()=>{
 const api=server(),local=storage();let key=0;
 const a=new OwnerSyncStore("owner",context,{text:""},api.transport,local,()=>`key${++key}`);
 await a.refresh();a.edit({text:"recover"});api.loseNextResponse();await assert.rejects(a.flush());assert.equal(api.version,1);a.close();
 const b=new OwnerSyncStore("owner",context,{text:""},api.transport,local,()=>`key${++key}`);
 await b.refresh();assert.equal(api.version,1);assert.equal(b.snapshot().value.text,"recover");assert.equal(b.snapshot().dirty,false);
});
test("owner switch closes stale responses and recovery remains owner scoped",async()=>{
 const local=storage();let complete;const delayed=()=>new Promise(resolve=>{complete=resolve;});
 const a=new OwnerSyncStore("owner-a",context,{text:""},delayed,local);const pending=a.refresh();a.edit({text:"private A"});a.close();
 complete({context,version:4,value:{text:"late A"},updated_at:null});await pending;
 assert.equal(a.snapshot().value.text,"private A");
 const b=new OwnerSyncStore("owner-b",context,{text:""},server().transport,local);await b.refresh();assert.equal(b.snapshot().value.text,"");
});
test("editing during a save persists the newer draft after acknowledgement",async()=>{
 const api=server();let finish;let hold=false;const transport=async(path,init)=>{
  const response=await api.transport(path,init);if(init&&hold){hold=false;await new Promise(resolve=>{finish=resolve;});}return response;
 };
 const a=new OwnerSyncStore("owner",context,{text:""},transport,storage());await a.refresh();a.edit({text:"first"});hold=true;
 const pending=a.flushAll();await new Promise(resolve=>setTimeout(resolve,0));a.edit({text:"second"});finish();await pending;
 assert.equal(api.version,2);assert.equal(a.snapshot().value.text,"second");assert.equal(a.snapshot().dirty,false);
});
test("old API 404 keeps recovery local and reports unsupported sync",async()=>{
 const a=new OwnerSyncStore("owner",context,{text:""},async()=>{throw {status:404,code:"HTTP_ERROR"};},storage());
 await a.refresh();a.edit({text:"local only"});await a.flush();assert.equal(a.snapshot().unsupported,true);assert.equal(a.snapshot().dirty,true);
});
