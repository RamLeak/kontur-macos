import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {createRequire} from "node:module";
import {dirname,join} from "node:path";
import {fileURLToPath} from "node:url";
const root=join(dirname(fileURLToPath(import.meta.url)),"..");
const require=createRequire(join(root,"package.json")),ts=require("typescript");
const config=JSON.parse(await new Promise(resolve=>{let body="";process.stdin.setEncoding("utf8");process.stdin.on("data",chunk=>body+=chunk);process.stdin.on("end",()=>resolve(body));}));
const source=await readFile(join(root,"lib/owner-sync-store.ts"),"utf8"),module={exports:{}};
new Function("module","exports","require",ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText)(module,module.exports,require);
const {OwnerSyncStore}=module.exports;
function storage(){const map=new Map();return {getItem:key=>map.get(key)??null,setItem:(key,value)=>map.set(key,value),removeItem:key=>map.delete(key)};}
function transport(token=config.token,expected="TEST_ONLY-owner"){
 return async(path,init)=>{
  const response=await fetch(config.url+"/api/v1"+path,{...init,headers:{"Content-Type":"application/json","X-Owner-Session":token,"X-Sync-Owner":expected}});
  const body=await response.json();if(!response.ok)throw {...body,status:response.status};return body;
 };
}
const request=transport();
const conversation=await request("/conversations",{method:"POST",body:JSON.stringify({title:"TEST_ONLY two real stores"})});
const context={kind:"composer",conversation_id:conversation.id};
const a=new OwnerSyncStore("TEST_ONLY-owner",context,{text:""},request,storage());
const recovery=storage(),b=new OwnerSyncStore("TEST_ONLY-owner",context,{text:""},request,recovery);
await Promise.all([a.refresh(),b.refresh()]);a.edit({text:"client A"});b.edit({text:"client B"});await a.flush();
await assert.rejects(b.flush(),error=>error.code==="VERSION_CONFLICT");assert.equal(b.snapshot().value.text,"client B");assert.equal(b.snapshot().remote.value.text,"client A");
b.resolve("local");await b.flush();await a.refresh();assert.equal(a.snapshot().value.text,"client B");assert.equal(a.snapshot().version,2);
let lose=true;const uncertain=async(path,init)=>{const response=await request(path,init);if(init&&lose){lose=false;throw {status:0,code:"API_UNREACHABLE"};}return response;};
const c=new OwnerSyncStore("TEST_ONLY-owner",context,{text:""},uncertain,recovery);await c.refresh();c.edit({text:"response lost"});await assert.rejects(c.flush());c.close();
const restarted=new OwnerSyncStore("TEST_ONLY-owner",context,{text:""},request,recovery);await restarted.refresh();assert.equal(restarted.snapshot().version,3);assert.equal(restarted.snapshot().dirty,false);
const prefs={kind:"preferences",base_version:0,idempotency_key:"TEST_ONLY-prefs",value:{theme:"dark"}};
await request("/sync/records",{method:"PUT",body:JSON.stringify(prefs)});
const stale=async(path,init)=>{
 const response=await fetch(config.url+"/api/v1"+path,{...init,headers:{"Content-Type":"application/json",
  "Cookie":`${config.cookieName}=${config.foreignCookie}`,"X-Sync-Owner":"TEST_ONLY-owner"}});
 const body=await response.json();if(!response.ok)throw {...body,status:response.status};return body;
};
await assert.rejects(stale("/sync/records?kind=preferences"),error=>error.status===401&&error.code==="OWNER_SESSION_CHANGED");
await assert.rejects(stale("/sync/records",{method:"PUT",body:JSON.stringify({...prefs,idempotency_key:"TEST_ONLY-stale"})}),error=>error.status===401);
const foreign=transport(config.foreignToken,"TEST_ONLY-foreign");assert.equal((await foreign("/sync/records?kind=preferences")).version,0);
assert.equal((await foreign("/sync/changes")).changes.length,0);
let release;const delayed=async(path,init)=>{const result=await request(path,init);await new Promise(resolve=>{release=resolve;});return result;};
const stopped=new OwnerSyncStore("TEST_ONLY-owner",context,{text:""},delayed,storage());const reading=stopped.refresh();while(!release)await new Promise(resolve=>setTimeout(resolve,1));stopped.close();release();await reading;assert.equal(stopped.snapshot().ready,false);
const newSource=await readFile(join(root,"lib/new-conversation-draft.ts"),"utf8"),newModule={exports:{}};
new Function("module","exports","require",ts.transpileModule(newSource,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText)(newModule,newModule.exports,require);
const {NewConversationDraft}=newModule.exports;let createdId,createCount=0;const records=new Map();
const newDraft=new NewConversationDraft("TEST_ONLY-owner",null,storage(),{
 create:async()=>{createCount++;const created=await request("/conversations",{method:"POST",body:JSON.stringify({title:"TEST_ONLY new unsent draft"})});return created.id;},
 save:async(id,text)=>{let record=records.get(id);if(!record){record=new OwnerSyncStore("TEST_ONLY-owner",{kind:"composer",conversation_id:id},{text:""},request,storage());records.set(id,record);}record.edit({text});await record.refresh();await record.flushAll();return !record.snapshot().dirty&&!record.snapshot().conflict;},
 ready:id=>{createdId=id;},
});newDraft.edit("unsent new chat across devices");await Promise.all([newDraft.flush(),newDraft.flush()]);assert.equal(createCount,1);assert.equal(newDraft.snapshot().dirty,false);
const createdMessages=await request(`/conversations/${createdId}/messages`);assert.deepEqual(Array.isArray(createdMessages)?createdMessages:createdMessages.items,[]);
const resumed=new OwnerSyncStore("TEST_ONLY-owner",{kind:"composer",conversation_id:createdId},{text:""},request,storage());await resumed.refresh();assert.equal(resumed.snapshot().value.text,"unsent new chat across devices");
let uncertainCreates=0;const uncertainNew=new NewConversationDraft("TEST_ONLY-owner","uncertain-project",storage(),{
 create:async()=>{uncertainCreates++;await request("/conversations",{method:"POST",body:JSON.stringify({title:"TEST_ONLY uncertain empty"})});throw new Error("TEST_ONLY lost create response");},save:async()=>false,ready:()=>{},
});uncertainNew.edit("local recoverable unsent");await assert.rejects(uncertainNew.flush());await assert.rejects(uncertainNew.flush());assert.equal(uncertainCreates,1);assert.equal(uncertainNew.localRecoverable(),true);
console.log("TEST_ONLY real HTTP API: two independent stores, dirty conflict, explicit resolution, lost-response restart replay, stale owner expectation with actual foreign v2cookie, foreign isolation, delayed closed-store response, new-chat actual server draft without messages/AI, uncertain create no-auto-retry: PASS");
