import {PYTHON_RUNTIME_HASHES} from "./browser-python-integrity";
import type {DeviceFile} from "./device-files";
import type {CodeRun} from "./types";

export const BROWSER_PYTHON_VERSION="314.0.7";
export const PYTHON_TIMEOUT_MS=30_000;
export const MAX_PYTHON_INPUT_BYTES=64_000_000;
const MAX_OUTPUT=200_000;
export interface BrowserPythonJob {result:Promise<CodeRun>;stop:()=>void}
interface RuntimeFiles {loader:string;engine:string;wasm:ArrayBuffer;stdlib:ArrayBuffer;lock:string}
let runtimePromise:Promise<RuntimeFiles>|null=null;
export function preloadBrowserPython(){if(!runtimePromise){runtimePromise=(async()=>{
 const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),20000);
 try{const names=["pyodide.mjs","pyodide.asm.mjs","pyodide.asm.wasm","python_stdlib.zip","pyodide-lock.json"];
 const r=await Promise.all(names.map(async name=>{const response=await fetch(`/python-runtime/${name}?v=${BROWSER_PYTHON_VERSION}`,{credentials:"omit",cache:"force-cache",signal:controller.signal});if(!response.ok)throw new Error("Python unavailable");
 const reader=response.body?.getReader();const chunks:Uint8Array[]=[];let size=0;if(reader){while(true){const part=await reader.read();if(part.done)break;size+=part.value.byteLength;if(size>12000000){await reader.cancel();throw new Error("Runtime too large");}chunks.push(part.value);}}else chunks.push(new Uint8Array(await response.arrayBuffer()));const bytes=new Uint8Array(chunks.reduce((n,c)=>n+c.byteLength,0));if(bytes.byteLength>12000000)throw new Error("Runtime too large");let offset=0;for(const c of chunks){bytes.set(c,offset);offset+=c.byteLength;}const hash=Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",bytes))).map(x=>x.toString(16).padStart(2,"0")).join("");if(hash!==PYTHON_RUNTIME_HASHES[name])throw new Error("Runtime integrity failed");return name.endsWith(".wasm")||name.endsWith(".zip")?bytes.buffer:new TextDecoder().decode(bytes);}));
 return {loader:r[0] as string,engine:r[1] as string,wasm:r[2] as ArrayBuffer,stdlib:r[3] as ArrayBuffer,lock:r[4] as string};}finally{clearTimeout(timer);controller.abort();}
 })();runtimePromise.catch(()=>{runtimePromise=null;});}return runtimePromise;}
// The opaque-origin iframe owns the worker. CSP permits local blob initialization,
// but no network, API, parent DOM, shared IndexedDB, cookies or storage authority.
// jsglobals is empty as defense in depth; CSP/opaque origin are the boundary.
export const PYTHON_FRAME_DOCUMENT=`<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline' blob: 'wasm-unsafe-eval'; worker-src blob:; connect-src blob:; style-src 'none'; img-src 'none'; form-action 'none'; base-uri 'none'"><script>
window.addEventListener('message',event=>{if(event.source!==parent||!event.ports[0])return;const port=event.ports[0];let worker;port.onmessage=e=>{if(e.data.type==='stop'){worker?.terminate();port.postMessage({type:'stopped'});return;}if(e.data.type!=='boot'||worker)return;const url=URL.createObjectURL(new Blob([e.data.worker],{type:'text/javascript'}));worker=new Worker(url);URL.revokeObjectURL(url);worker.onmessage=m=>port.postMessage(m.data);worker.onerror=e=>{console.warn('Python initialization failed',e.message);port.postMessage({type:'failed',message:'Не удалось запустить Python.'});};worker.postMessage(e.data.payload);};port.start();port.postMessage({type:'connected'});},{once:true});
</script>`;
const WORKER=String.raw`
self.onmessage=async event=>{
 const {runtime,code,files}=event.data;let stdout='',stderr='',overflow=false;const cap=200000;
 const append=(kind,text)=>{if(overflow)return;const value=String(text)+'\n';if(stdout.length+stderr.length+value.length>cap){overflow=true;self.postMessage({type:'output-limit',stdout,stderr});return;}if(kind==='out')stdout+=value;else stderr+=value;};
 const cleanTraceback=error=>{const raw=String(error?.message||error||'Python execution failed').replace(/\r\n?/g,'\n');const isJavaScriptStackLine=line=>{const trimmed=line.trim();if(/^wasm-function\[\d+\](?::0x[0-9a-f]+)?$/i.test(trimmed))return true;if(!/^at\s+/i.test(trimmed))return false;return /(?:wasm-function\[|\.(?:m?js|wasm)(?::|\)|$))/i.test(trimmed)||/\([^)]*:\d+:\d+\)\s*$/.test(trimmed);};const pythonFramePath=line=>line.match(/^\s*File\s+[\"']([^\"']+)[\"'],\s+line\s+\d+(?:,\s+in\s+.*)?$/)?.[1]||null;const isInternalPyodideFrame=path=>{const normalized=path.replace(/\\/g,'/');return /(?:^|\/)_?pyodide(?:\/|$)/i.test(normalized);};const withoutInternalPyodideFrames=lines=>{const kept=[];for(let index=0;index<lines.length;index+=1){const path=pythonFramePath(lines[index]);if(!path||!isInternalPyodideFrame(path)){kept.push(lines[index]);continue;}index+=1;while(index<lines.length){if(pythonFramePath(lines[index]))break;const line=lines[index];if(line&&!/^\s/.test(line))break;index+=1;}index-=1;}return kept;};const lines=raw.split('\n').filter(line=>!isJavaScriptStackLine(line));const start=lines.findIndex(line=>line.includes('Traceback (most recent call last):')||/\bFile [\"'](?:main\.py|<exec>)/.test(line));const selected=start<0?lines:withoutInternalPyodideFrames(lines.slice(start));return selected.map(line=>line.replace(/^PythonError:\s*/,'' )).join('\n').trim().slice(0,200000);};
 try{
  const loaderUrl=URL.createObjectURL(new Blob([runtime.loader],{type:'text/javascript'}));const engineUrl=URL.createObjectURL(new Blob([runtime.engine],{type:'text/javascript'}));
  const {loadPyodide}=await import(loaderUrl);const {default:engine}=await import(engineUrl);
  const localFetch=self.fetch.bind(self);
  self.fetch=(url,options)=>String(url)==='https://invalid.local/pyodide.asm.wasm'?Promise.resolve(new Response(runtime.wasm,{headers:{'Content-Type':'application/wasm'}})):String(url).startsWith('blob:')?localFetch(url,options):Promise.reject(new Error('Network unavailable'));
  const createPyodideModule=engine;
  const stdLibURL=URL.createObjectURL(new Blob([runtime.stdlib]));
  const py=await loadPyodide({indexURL:'https://invalid.local/',createPyodideModule,stdLibURL,lockFileContents:runtime.lock,packageBaseUrl:'https://invalid.local/',jsglobals:Object.freeze(Object.create(null)),stdout:()=>{},stderr:()=>{},stdin:()=>null});
  py.FS.mkdir('/work');py.FS.chdir('/work');py.FS.symlink('/work','/input');
  for(const file of files)py.FS.writeFile('/work/'+file.name,new Uint8Array(file.bytes));
  py.setStdout({batched:text=>append('out',text)});py.setStderr({batched:text=>append('err',text)});
  const globals=py.toPy({__name__:'__main__'});self.postMessage({type:'ready'});
  const started=performance.now();let exit=0;
  try{await py.runPythonAsync(code,{globals,filename:'main.py'});}catch(error){exit=1;append('err',cleanTraceback(error));}
  globals.destroy();self.postMessage({type:'done',stdout,stderr:overflow?stderr+'Вывод слишком большой.\n':stderr,exitCode:overflow?1:exit,duration:Math.round(performance.now()-started)});
 }catch(error){self.postMessage({type:'failed',message:'Не удалось подготовить Python. Повторите запуск.'});}
};`;
export function startBrowserPython(code:string,files:DeviceFile[],codeVersion:number,timeoutMs=PYTHON_TIMEOUT_MS):BrowserPythonJob{
 let finish:(run:CodeRun)=>void=()=>{};let frame:HTMLIFrameElement|null=null;let channel:MessageChannel|null=null;let timer:ReturnType<typeof setTimeout>|null=null;let ended=false;let stoppedResult:(()=>void)|null=null;const id='device-'+crypto.randomUUID();let started=performance.now();
 const result=new Promise<CodeRun>(resolve=>{finish=resolve;});
 const complete=(status:CodeRun['status'],stdout='',stderr='',exit:number|null=null,duration:number|null=null)=>{if(ended)return;ended=true;if(timer)clearTimeout(timer);channel?.port1.close();channel?.port2.close();frame?.remove();finish({id,code_version:codeVersion,status,stdout:stdout.slice(0,MAX_OUTPUT),stderr:stderr.slice(0,MAX_OUTPUT),exit_code:exit,duration_ms:duration,created_at:new Date().toISOString()});};
 const terminateAndComplete=(...args:Parameters<typeof complete>)=>{if(ended||stoppedResult)return;stoppedResult=()=>complete(...args);channel?.port1.postMessage({type:'stop'});setTimeout(()=>stoppedResult?.(),250);};
 if(code.length>200000||files.length>16||files.some(f=>!f.name||f.name.length>255||/[\\/\x00-\x1f]/.test(f.name)||f.name==='.'||f.name==='..')||files.reduce((n,f)=>n+f.bytes.byteLength,0)>MAX_PYTHON_INPUT_BYTES){complete('failed','',"Программа или файлы превышают доступный объём.",1);return {result,stop:()=>{}};}
 timer=setTimeout(()=>complete('timed_out','',"Подготовка Python заняла слишком много времени. Повторите запуск.",null),60000);
 void preloadBrowserPython().then(runtime=>{
  if(ended)return;frame=document.createElement('iframe');frame.setAttribute('sandbox','allow-scripts');frame.setAttribute('aria-hidden','true');frame.tabIndex=-1;frame.style.display='none';frame.srcdoc=PYTHON_FRAME_DOCUMENT;
  channel=new MessageChannel();channel.port1.onmessage=event=>{const data=event.data;
   if(data?.type==='connected'){channel?.port1.postMessage({type:'boot',worker:WORKER,payload:{runtime,code,files:files.map(f=>({name:f.name,bytes:f.bytes}))}});}
   else if(data?.type==='stopped'){stoppedResult?.();}
   else if(data?.type==='output-limit'){terminateAndComplete('failed',typeof data.stdout==='string'?data.stdout:'',"Вывод слишком большой. Программа остановлена.",1);}
   else if(data?.type==='ready'){if(timer)clearTimeout(timer);started=performance.now();timer=setTimeout(()=>{terminateAndComplete('timed_out','',"Программа остановлена: превышено время выполнения.",null,Math.round(performance.now()-started));},Math.max(100,Math.min(120000,timeoutMs)));}
   else if(data?.type==='done'){if(typeof data.stdout==='string'&&typeof data.stderr==='string'&&typeof data.duration==='number'&&(data.exitCode===0||data.exitCode===1))terminateAndComplete(data.exitCode===0?'completed':'failed',data.stdout,data.stderr,data.exitCode,data.duration);else complete('failed','',"Не удалось получить результат программы.",1);}
   else if(data?.type==='failed')terminateAndComplete('failed','',typeof data.message==='string'?data.message:"Не удалось запустить Python.",1);
  };
  frame.onload=()=>{frame?.contentWindow?.postMessage({type:'connect'},'*',[channel!.port2]);};document.body.appendChild(frame);
 }).catch(()=>complete('unavailable','',"Не удалось подготовить Python. Повторите запуск."));
 return {result,stop:()=>{terminateAndComplete('failed','',"Программа остановлена.",null,Math.round(performance.now()-started));}};
}
