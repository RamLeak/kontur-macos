"use client";
export interface ExamUiState{itemId:string|null;flags:Record<string,boolean>;surfaces:Record<string,"code"|"board">}
export interface StudyCheckpoint{runId:string;question:string}
const fallbackKeys=new Map<string,{fingerprint:string;key:string}>();
function read(key:string):unknown{if(typeof window==="undefined")return null;try{return JSON.parse(localStorage.getItem(key)||"null");}catch{return null;}}
function write(key:string,value:unknown){if(typeof window==="undefined")return;try{localStorage.setItem(key,JSON.stringify(value));}catch{}}
function record(value:unknown):value is Record<string,unknown>{return Boolean(value&&typeof value==="object"&&!Array.isArray(value));}
export function readExamUiState(scope:string,itemIds:string[]):ExamUiState{
 const raw=read("kontur:exam-ui:v1:"+scope);const result:ExamUiState={itemId:null,flags:{},surfaces:{}};
 if(!record(raw))return result;
 result.itemId=typeof raw.itemId==="string"&&itemIds.includes(raw.itemId)?raw.itemId:null;
 for(const id of itemIds){if(record(raw.flags)&&raw.flags[id]===true)result.flags[id]=true;if(record(raw.surfaces)&&(raw.surfaces[id]==="code"||raw.surfaces[id]==="board"))result.surfaces[id]=raw.surfaces[id];}
 return result;
}
export function saveExamUiState(scope:string,state:ExamUiState){if(scope)write("kontur:exam-ui:v1:"+scope,state);}
export function readStudyCheckpoint(scope:string):StudyCheckpoint|null{const raw=read("kontur:study-checkpoint:v1:"+scope);return record(raw)&&typeof raw.runId==="string"&&raw.runId.length>0&&raw.runId.length<160&&typeof raw.question==="string"&&raw.question.length<=20000?{runId:raw.runId,question:raw.question}:null;}
export function saveStudyCheckpoint(scope:string,value:StudyCheckpoint){if(scope&&value.runId&&value.question.length<=20000)write("kontur:study-checkpoint:v1:"+scope,value);}
export function studyRequestKey(scope:string,fingerprint:string):string{
 const raw=read("kontur:study-pending:v1:"+scope);
 const existing=record(raw)&&typeof raw.fingerprint==="string"&&typeof raw.key==="string"?{fingerprint:raw.fingerprint,key:raw.key}:fallbackKeys.get(scope);
 if(existing?.fingerprint===fingerprint)return existing.key;
 const key="study-ai:"+crypto.randomUUID();const value={fingerprint,key};fallbackKeys.set(scope,value);write("kontur:study-pending:v1:"+scope,value);return key;
}
export function clearStudyRequestKey(scope:string){fallbackKeys.delete(scope);if(typeof window!=="undefined")try{localStorage.removeItem("kontur:study-pending:v1:"+scope);}catch{}}
