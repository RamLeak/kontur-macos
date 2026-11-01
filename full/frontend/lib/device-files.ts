import {API_BASE} from "./api";
import {localFetch} from "./local-fetch";
import type {AssetRef} from "./types";

export interface DeviceFile {name:string;bytes:Uint8Array;sha256:string;mediaType:string;origin?:"task"|"local";sourceHash?:string;converterVersion?:string;sheetName?:string}
const DB="kontur-device-files-v1", STORE="files";
interface PendingDownload {promise:Promise<DeviceFile>;controller:AbortController;users:number}
const pending=new Map<string,PendingDownload>();
interface PendingTexts {promise:Promise<DeviceFile[]>;controller:AbortController;users:number}
const pendingTexts=new Map<string,PendingTexts>();
let cacheGeneration=0;let activeDownloads=0;const downloadWaiters:Array<()=>void>=[];
async function downloadSlot<T>(work:()=>Promise<T>):Promise<T>{if(activeDownloads>=4)await new Promise<void>(resolve=>downloadWaiters.push(resolve));else activeDownloads++;try{return await work();}finally{const next=downloadWaiters.shift();if(next)next();else activeDownloads--;}}
const memory=new Map<string,{file:DeviceFile;touched:number}>();
export const MAX_FILE_BYTES=64_000_000;
export const MAX_DEVICE_FILES=16;
export function safeDeviceFilename(name:string){return name.length>0&&name.length<=255&&!/[\\/\x00-\x1f]/.test(name)&&name!=="."&&name!=="..";}
export async function fileHash(bytes:Uint8Array){return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",new Uint8Array(bytes)))).map(x=>x.toString(16).padStart(2,"0")).join("");}
export async function readLocalDeviceFile(file:Pick<File,"name"|"type"|"size"|"arrayBuffer">):Promise<DeviceFile>{
 if(!safeDeviceFilename(file.name))throw new Error("Имя локального файла недопустимо. Выберите файл без папок и управляющих символов.");
 if(!Number.isSafeInteger(file.size)||file.size<0||file.size>MAX_FILE_BYTES)throw new Error(`Локальный файл слишком большой. Выберите файл не больше ${Math.round(MAX_FILE_BYTES/1024/1024)} МБ.`);
 let bytes:Uint8Array;try{bytes=new Uint8Array(await file.arrayBuffer());}catch{throw new Error("Не удалось прочитать локальный файл. Проверьте доступ к нему и выберите файл ещё раз.");}
 if(bytes.byteLength>MAX_FILE_BYTES)throw new Error(`Локальный файл слишком большой. Выберите файл не больше ${Math.round(MAX_FILE_BYTES/1024/1024)} МБ.`);
 return {name:file.name,bytes,sha256:await fileHash(bytes),mediaType:file.type||"application/octet-stream",origin:"local"};
}
export function validateDeviceFileSet(files:readonly DeviceFile[]):void{
 if(files.length>MAX_DEVICE_FILES)throw new Error("Слишком много файлов для локального Python. Удалите лишние вложения.");
 let total=0;const names=new Set<string>();
 for(const file of files){
  if(!safeDeviceFilename(file.name))throw new Error("Имя файла недоступно для Python.");
  if(!(file.bytes instanceof Uint8Array)||file.bytes.byteLength>MAX_FILE_BYTES)throw new Error("Файл слишком большой для локального Python.");
  total+=file.bytes.byteLength;if(total>MAX_FILE_BYTES)throw new Error("Файлы задания превышают доступный объём локального Python.");
  const name=file.name.toLowerCase();if(names.has(name))throw new Error("В локальном Python совпадают имена файлов. Выберите другое имя или удалите дубликат.");names.add(name);
 }
}
function openDb():Promise<IDBDatabase>{return new Promise((resolve,reject)=>{const req=indexedDB.open(DB,2);
 req.onupgradeneeded=()=>{const db=req.result;if(!db.objectStoreNames.contains(STORE))db.createObjectStore(STORE);if(!db.objectStoreNames.contains("metadata")){const metadata=db.createObjectStore("metadata");const cursor=req.transaction!.objectStore(STORE).openCursor();cursor.onsuccess=()=>{const row=cursor.result;if(!row)return;if(row.value?.file?.bytes)metadata.put({size:row.value.file.bytes.byteLength,touched:row.value.touched||0},row.key);row.continue();};}};
 let blocked=false;req.onsuccess=()=>{if(blocked)req.result.close();else resolve(req.result);};req.onerror=()=>reject(req.error);req.onblocked=()=>{blocked=true;reject(new Error("Хранилище занято. Закройте другие вкладки и повторите."));};});}
async function touchCachedMetadata(key:string){
 let db:IDBDatabase|null=null;
 try{
  db=await openDb();
  await new Promise<void>((resolve,reject)=>{
   const tx=db!.transaction("metadata","readwrite");const store=tx.objectStore("metadata");const request=store.get(key);
   request.onsuccess=()=>{if(request.result)store.put({...request.result,touched:Date.now()},key);};
   tx.oncomplete=()=>resolve();tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error);
  });
 }catch{/* Cache recency is best-effort when device storage is unavailable. */}finally{db?.close();}
}
async function readCached(key:string):Promise<DeviceFile|null>{
 const local=memory.get(key);if(local&&await fileHash(local.file.bytes)===local.file.sha256){local.touched=Date.now();await touchCachedMetadata(key);return local.file;}
 try{const db=await openDb();const value=await new Promise<{file:DeviceFile;touched:number}|undefined>((resolve,reject)=>{const req=db.transaction(STORE).objectStore(STORE).get(key);req.onsuccess=()=>resolve(req.result);req.onerror=()=>reject(req.error);});
 if(value&&await fileHash(value.file.bytes)===value.file.sha256){const tx=db.transaction("metadata","readwrite");tx.objectStore("metadata").put({size:value.file.bytes.byteLength,touched:Date.now()},key);db.close();return value.file;}db.close();}catch{}return null;
}
export function deviceCacheLimitBytes(){try{const n=JSON.parse(localStorage.getItem("kontur:interface-preferences:v1")||"{}").fileCacheMb;return (Number.isFinite(n)?Math.min(1024,Math.max(32,n)):256)*1024*1024;}catch{return 256*1024*1024;}}
async function cacheFile(key:string,file:DeviceFile,generation=cacheGeneration){
 if(generation!==cacheGeneration)return;
 const limit=deviceCacheLimitBytes();memory.set(key,{file,touched:Date.now()});
 let size=Array.from(memory.values()).reduce((n,v)=>n+v.file.bytes.byteLength,0);
 for(const [k,v] of [...memory.entries()].sort((a,b)=>a[1].touched-b[1].touched)){if(size<=limit)break;memory.delete(k);size-=v.file.bytes.byteLength;}
 try{const db=await openDb();if(generation!==cacheGeneration){db.close();return;}await new Promise<void>((resolve,reject)=>{const tx=db.transaction([STORE,"metadata"],"readwrite");const touched=Date.now();tx.objectStore(STORE).put({file,touched},key);tx.objectStore("metadata").put({size:file.bytes.byteLength,touched},key);tx.oncomplete=()=>resolve();tx.onerror=()=>reject(tx.error);});
 const entries=await new Promise<Array<{key:IDBValidKey;size:number;touched:number}>>((resolve,reject)=>{const result:Array<{key:IDBValidKey;size:number;touched:number}>=[];const req=db.transaction("metadata").objectStore("metadata").openCursor();req.onsuccess=()=>{const c=req.result;if(c){result.push({key:c.key,size:c.value.size,touched:c.value.touched});c.continue();}else resolve(result);};req.onerror=()=>reject(req.error);});
 let total=entries.reduce((n,v)=>n+v.size,0);const tx=db.transaction([STORE,"metadata"],"readwrite");for(const v of entries.sort((a,b)=>a.touched-b.touched)){if(total<=limit)break;tx.objectStore(STORE).delete(v.key);tx.objectStore("metadata").delete(v.key);total-=v.size;}db.close();}catch{/* The current file remains usable when persistent storage is unavailable. */}
}
export async function clearDeviceFileCache(){cacheGeneration++;memory.clear();
 try{const db=await openDb();await new Promise<void>((resolve,reject)=>{const tx=db.transaction([STORE,"metadata"],"readwrite");tx.objectStore(STORE).clear();tx.objectStore("metadata").clear();tx.oncomplete=()=>resolve();tx.onerror=()=>reject(tx.error);});db.close();}
 catch{throw new Error("Не удалось очистить сохранённые файлы. Попробуйте ещё раз.");}
}
const DEVICE_HTTP_ERROR_CODES=new Set(["ASSET_TOO_LARGE","ASSET_HASH_MISMATCH","ASSET_INTEGRITY_FAILED","ASSET_NOT_CACHED","ASSET_NOT_READY","ASSET_QUARANTINED","ASSET_UNAVAILABLE","SOURCE_RIGHTS_REQUIRED","NOT_FOUND","column_limit","row_limit","sheet_limit","SPREADSHEET_COLUMN_LIMIT","SPREADSHEET_SHEET_LIMIT"]);
function transportErrorMessage(reason:unknown){
 const code=reason&&typeof reason==="object"&&typeof (reason as {code?:unknown}).code==="string"?(reason as {code:string}).code:"";
 if(code==="LOCAL_DESKTOP_REQUIRED"||code==="local_backend_unavailable"||code==="local_backend_starting")return "Не удалось открыть вложение в локальном приложении. Перезапустите Контур и повторите; можно прикрепить локальную копию.";
 if(code==="LOCAL_RESPONSE_TOO_LARGE")return "Ответ с вложением слишком большой для локального приложения. Выберите файл меньшего размера или прикрепите локальную копию.";
 return "Не удалось открыть вложение в локальном приложении. Проверьте состояние Контур и повторите; можно прикрепить локальную копию.";
}
function httpErrorMessage(status:number,code:string|null){
 if(code==="ASSET_TOO_LARGE"||status===413)return "Вложение слишком большое для локального Python. Выберите файл меньшего размера или прикрепите допустимую локальную копию.";
 if(code==="ASSET_HASH_MISMATCH"||code==="ASSET_INTEGRITY_FAILED")return "Не удалось подтвердить целостность вложения. Повторите загрузку; если ошибка повторяется, прикрепите неповреждённую локальную копию.";
 if(code==="ASSET_NOT_CACHED"||code==="ASSET_NOT_READY"||code==="ASSET_UNAVAILABLE")return "Вложение пока недоступно для этого условия. Повторите загрузку позже или прикрепите локальную копию.";
 if(code==="column_limit"||code==="row_limit"||code==="sheet_limit"||code==="SPREADSHEET_COLUMN_LIMIT"||code==="SPREADSHEET_SHEET_LIMIT")return "Таблица слишком широкая для подготовки TXT. Исходное вложение не изменено; прикрепите локальную копию, если нужен другой способ разбора.";
 if(code==="ASSET_QUARANTINED"||code==="SOURCE_RIGHTS_REQUIRED")return "Вложение недоступно для этого условия. Откройте условие заново или используйте локальную копию.";
 if(code==="NOT_FOUND"||status===404)return "Вложение не найдено в этом условии. Откройте условие заново; банковские файлы не изменены.";
 if(status===401||status===403)return "Вложение недоступно в текущей сессии. Перезапустите Контур и откройте условие снова.";
 return "Не удалось загрузить вложение. Повторите попытку; если ошибка повторяется, прикрепите локальную копию.";
}
async function responseErrorCode(response:Response):Promise<string|null>{
 try{
  const raw=await response.clone().json();const detail=raw&&typeof raw==="object"?(raw as {detail?:unknown}).detail:null;
  const code=raw&&typeof raw==="object"&&typeof (raw as {code?:unknown}).code==="string"?(raw as {code:string}).code:detail&&typeof detail==="object"&&typeof (detail as {code?:unknown}).code==="string"?(detail as {code:string}).code:null;
  return code&&DEVICE_HTTP_ERROR_CODES.has(code)?code:null;
 }catch{return null;}
}
async function checkedResponse(path:string,signal?:AbortSignal){let response:Response;try{response=await localFetch(API_BASE+path,{cache:"no-store",signal});}catch(reason){if(signal?.aborted)throw reason;throw new TypeError(transportErrorMessage(reason));}if(!response.ok)throw new Error(httpErrorMessage(response.status,await responseErrorCode(response)));return response;}
export async function loadDeviceAsset(asset:AssetRef,revisionHash="",signal?:AbortSignal):Promise<DeviceFile>{
 signal?.throwIfAborted();
 if(!safeDeviceFilename(asset.name))throw new Error("Имя файла недоступно для Python.");
 const prefix=revisionHash+":"+asset.id+":";const pinKey="kontur:device-file-pin:"+prefix;
 let expected=asset.available?asset.content_hash:null;
 if(!expected){
  try{const response=await checkedResponse(`/assets/${encodeURIComponent(asset.id)}/device-metadata`,signal);const metadata=await response.json();
   if(metadata.asset_id!==asset.id||metadata.pinned_sha256!==null&&!/^[a-f0-9]{64}$/.test(metadata.pinned_sha256))throw new Error("Не удалось подтвердить файл.");expected=metadata.pinned_sha256;
  }catch(reason){
   // Offline may reuse only a digest previously confirmed by authenticated
   // device delivery. Authorization/HTTP/integrity failures never fall back.
   if(reason instanceof TypeError&&!signal?.aborted){let lastPin:string|null=null;try{lastPin=localStorage.getItem(pinKey);}catch{}
    if(lastPin&&/^[a-f0-9]{64}$/.test(lastPin)){const offline=await readCached(prefix+lastPin);if(offline)return offline;}}
   throw reason;
  }
 }
 const key=prefix+(expected||"source");
 const cached=expected?await readCached(key):null;if(cached){signal?.throwIfAborted();if(expected&&cached.sha256!==expected)throw new Error("Файл изменился. Откройте задание заново.");return cached;}
 // Shared consumers keep a single bounded fetch alive. The final aborted
 // consumer cancels queued work and the response stream.
 let entry=pending.get(key);if(entry?.controller.signal.aborted)entry=undefined;
 if(!entry){const generation=cacheGeneration;const controller=new AbortController();
  const promise=downloadSlot(async()=>{controller.signal.throwIfAborted();const response=await checkedResponse(`/assets/${encodeURIComponent(asset.id)}/device`,controller.signal);
   const advertised=response.headers.get("X-Content-Hash");const length=Number(response.headers.get("Content-Length")||0);if(length>MAX_FILE_BYTES){await response.body?.cancel();throw new Error("Файл слишком большой.");}
   const reader=response.body?.getReader();const chunks:Uint8Array[]=[];let size=0;
   if(reader){while(true){const part=await reader.read();if(part.done)break;size+=part.value.byteLength;if(size>MAX_FILE_BYTES){await reader.cancel();throw new Error("Файл слишком большой.");}chunks.push(part.value);}}
   else chunks.push(new Uint8Array(await response.arrayBuffer()));
   const bytes=new Uint8Array(chunks.reduce((n,c)=>n+c.byteLength,0));if(bytes.length>MAX_FILE_BYTES)throw new Error("Файл слишком большой.");let offset=0;for(const c of chunks){bytes.set(c,offset);offset+=c.byteLength;}
   const sha256=await fileHash(bytes);if(!advertised||sha256!==advertised||expected&&sha256!==expected)throw new Error("Не удалось подтвердить целостность файла.");
   const file={name:asset.name,bytes,sha256,mediaType:response.headers.get("Content-Type")||asset.media_type};
   if(generation===cacheGeneration&&!controller.signal.aborted){await cacheFile(prefix+sha256,file,generation);try{localStorage.setItem(pinKey,sha256);}catch{}}return file;
  });
  entry={promise,controller,users:0};pending.set(key,entry);const created=entry;promise.finally(()=>{if(pending.get(key)===created)pending.delete(key);}).catch(()=>undefined);
 }
 entry.users++;const current=entry;
 return new Promise<DeviceFile>((resolve,reject)=>{let settled=false;
  const finish=(error:unknown,file?:DeviceFile)=>{if(settled)return;settled=true;signal?.removeEventListener("abort",abort);current.users--;if(current.users===0)current.controller.abort();if(error)reject(error);else resolve(file!);};
  const abort=()=>finish(new DOMException("Загрузка отменена.","AbortError"));signal?.addEventListener("abort",abort,{once:true});if(signal?.aborted){abort();return;}
  current.promise.then(file=>finish(null,file),error=>finish(error));
 });
}
export async function loadDeviceAssetById(assetId:string,revisionHash="",signal?:AbortSignal):Promise<DeviceFile>{
 const response=await checkedResponse(`/assets/${encodeURIComponent(assetId)}/device-metadata`,signal);const metadata=await response.json();
 if(metadata.asset_id!==assetId||typeof metadata.filename!=="string"||!safeDeviceFilename(metadata.filename)||metadata.pinned_sha256!==null&&!/^[a-f0-9]{64}$/.test(metadata.pinned_sha256))throw new Error("Не удалось загрузить иллюстрацию.");
 return loadDeviceAsset({id:assetId,name:metadata.filename,media_type:"application/octet-stream",size_bytes:null,download_url:"",available:typeof metadata.pinned_sha256==="string",cache_state:"metadata_only",content_hash:metadata.pinned_sha256,rights_status:null,canonical_url:null,progress_percent:null},revisionHash,signal);
}
export async function loadDeviceTexts(asset:AssetRef,revisionHash="",signal?:AbortSignal):Promise<DeviceFile[]>{
 const generation=cacheGeneration;
 const original=await loadDeviceAsset(asset,revisionHash,signal);
 const key=revisionHash+":"+asset.id+":"+original.sha256+":type9-tsv-v3";
 const countKey=key+":count";let count=0;
 try{count=Number(localStorage.getItem(countKey)||0);}catch{}
 if(count>0&&count<=MAX_DEVICE_FILES){
  const cached=await Promise.all(Array.from({length:count},(_,i)=>readCached(key+":"+i)));
  signal?.throwIfAborted();if(cached.every(Boolean))return cached as DeviceFile[];
 }
 const pendingKey=key+":generation:"+generation;
 let entry=pendingTexts.get(pendingKey);if(entry?.controller.signal.aborted)entry=undefined;
 if(!entry){
  const controller=new AbortController();
  const promise=downloadSlot(async()=>{
   controller.signal.throwIfAborted();
   const response=await checkedResponse(`/assets/${encodeURIComponent(asset.id)}/device-text`,controller.signal);
   const raw=await response.json();
   if(raw.source_sha256!==original.sha256||!Array.isArray(raw.files)||!raw.files.length||raw.files.length>MAX_DEVICE_FILES)throw new Error("Исходная таблица изменилась. Повторите загрузку.");
   let total=0;const files:DeviceFile[]=[];
   for(const f of raw.files){
    if(!safeDeviceFilename(f.filename)||f.converter_version!=="type9-tsv-v3"||typeof f.content_base64!=="string"||f.content_base64.length>MAX_FILE_BYTES*1.4)throw new Error("Не удалось подготовить TXT.");
    const bytes=Uint8Array.from(atob(f.content_base64),(c:string)=>c.charCodeAt(0));total+=bytes.length;
    if(total>MAX_FILE_BYTES||await fileHash(bytes)!==f.content_hash)throw new Error("Не удалось подтвердить TXT.");
    files.push({name:f.filename,bytes,sha256:f.content_hash,mediaType:"text/plain;charset=utf-8",sourceHash:original.sha256,converterVersion:f.converter_version,sheetName:f.sheet_name});
   }
   controller.signal.throwIfAborted();
   if(generation===cacheGeneration){for(let i=0;i<files.length;i++)await cacheFile(key+":"+i,files[i],generation);if(generation===cacheGeneration){try{localStorage.setItem(countKey,String(files.length));}catch{}}}
   return files;
  });
  entry={promise,controller,users:0};pendingTexts.set(pendingKey,entry);const created=entry;
  promise.finally(()=>{if(pendingTexts.get(pendingKey)===created)pendingTexts.delete(pendingKey);}).catch(()=>undefined);
 }
 entry.users++;const current=entry;
 return new Promise<DeviceFile[]>((resolve,reject)=>{
  let settled=false;
  const finish=(error:unknown,files?:DeviceFile[])=>{if(settled)return;settled=true;signal?.removeEventListener("abort",abort);current.users--;if(current.users===0)current.controller.abort();if(error)reject(error);else resolve(files!);};
  const abort=()=>finish(new DOMException("Загрузка отменена.","AbortError"));
  signal?.addEventListener("abort",abort,{once:true});if(signal?.aborted){abort();return;}
  current.promise.then(files=>finish(null,files),error=>finish(error));
 });
}

export function isDeviceImage(asset:AssetRef){return asset.media_type.startsWith("image/")||/\.(png|jpe?g|gif|webp)$/i.test(asset.name);}
export async function loadTaskDeviceFiles(assets:AssetRef[],lineNumber:number,revisionHash:string,signal?:AbortSignal,localFiles:readonly DeviceFile[]=[]):Promise<DeviceFile[]>{
 validateDeviceFileSet(localFiles);
 const localByName=new Map(localFiles.map(file=>[file.name.toLowerCase(),file]));
 const selected=assets.filter(a=>!isDeviceImage(a));if(selected.length>MAX_DEVICE_FILES)throw new Error("Слишком много файлов задания.");
 const files:DeviceFile[]=[];const selectedNames=new Set<string>();
 for(const asset of selected){
  signal?.throwIfAborted();const local=localByName.get(asset.name.toLowerCase());
  if(local){files.push(local);selectedNames.add(local.name.toLowerCase());continue;}
  const rows=lineNumber===9&&/\.(csv|xlsx?|ods)$/i.test(asset.name)?await loadDeviceTexts(asset,revisionHash,signal):[await loadDeviceAsset(asset,revisionHash,signal)];
  for(const row of rows){const localRow=localByName.get(row.name.toLowerCase());files.push(localRow??row);selectedNames.add(row.name.toLowerCase());}
 }
 for(const local of localFiles)if(!selectedNames.has(local.name.toLowerCase()))files.push(local);
 validateDeviceFileSet(files);return files;
}
export function deviceAssetUrl(file:DeviceFile){return URL.createObjectURL(new Blob([new Uint8Array(file.bytes)],{type:file.mediaType}));}
export function downloadDeviceFile(file:DeviceFile){const url=deviceAssetUrl(file);const a=document.createElement("a");a.href=url;a.download=file.name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
