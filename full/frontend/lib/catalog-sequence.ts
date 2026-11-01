import type {TaskCatalogQuery} from "./types";
export interface CatalogSequence {revisionIds:string[];total:number|null;nextCursor:string|null;query:TaskCatalogQuery;}
const KEY="kontur:catalog-sequence:v2";
export function readCatalogSequence():CatalogSequence|null{
 if(typeof window==="undefined")return null;
 try{const value=JSON.parse(sessionStorage.getItem(KEY)||"null");if(!value||!Array.isArray(value.revisionIds)||!value.revisionIds.every((id:unknown)=>typeof id==="string")||!value.query)return null;return value;}catch{return null;}
}
export function rememberCatalogSequence(value:CatalogSequence){try{sessionStorage.setItem(KEY,JSON.stringify(value));}catch{}}
export function sequencePosition(sequence:CatalogSequence|null,revisionId:string){return sequence?sequence.revisionIds.indexOf(revisionId):-1;}
