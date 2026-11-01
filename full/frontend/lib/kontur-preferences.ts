"use client";
import {useEffect, useState} from "react";
import {isLocalDesktop} from "./local-fetch";
import type {Effort, ModelId, TutorMode} from "@/lib/types";
export interface KonturPreferences {
  theme: "system" | "light" | "dark";
  reducedMotion: boolean;
  textSize: "standard" | "large";
  density: "comfortable" | "compact";
  autoHideNavigation: boolean;
  rightPanelVisible: boolean;
  defaultTaskType: number;
  restoreWorkspace: boolean;
  editorTheme: "interface" | "light" | "charcoal";
  editorFontSize: 12 | 14 | 16;
  editorWrap: boolean;
  editorTabSize: 2 | 4 | 8;
  editorLineNumbers: boolean;
  outputHeight: number;
  boardBackground: "blank" | "grid" | "dots";
  boardColor: string;
  boardWidth: number;
  boardOpacity: number;
  boardMarkerColor: string;
  boardMarkerWidth: number;
  boardMarkerOpacity: number;
  drawingSmoothing: number;
  fileCacheMb: 32 | 64 | 128 | 256 | 512 | 1024;
  assistantWidth: "narrow" | "standard" | "wide";
  hintMode: "hint" | "explain";
  model: ModelId;
  effort: Effort;
  tutorMode: TutorMode;
}
export const PREFERENCES_CHANGED = "kontur:preferences-changed";
export const DEFAULT_PREFERENCES: KonturPreferences = {theme:"system",reducedMotion:false,textSize:"standard",density:"comfortable",autoHideNavigation:true,
 rightPanelVisible:true,defaultTaskType:14,restoreWorkspace:true,editorTheme:"interface",editorFontSize:14,editorWrap:false,editorTabSize:4,editorLineNumbers:true,outputHeight:160,
 boardBackground:"blank",boardColor:"#1D1D1F",boardWidth:3,boardOpacity:1,boardMarkerColor:"#F2C94C",boardMarkerWidth:18,boardMarkerOpacity:1,drawingSmoothing:.65,
 fileCacheMb:256,assistantWidth:"standard",hintMode:"hint",model:"auto",effort:"medium",tutorMode:"hint"};
const KEY = "kontur:interface-preferences:v1";
let preferenceOwner:string|null|undefined;
const SHARED_KEYS=["theme","reducedMotion","textSize","defaultTaskType","restoreWorkspace","editorTheme","editorFontSize","editorWrap","editorTabSize","editorLineNumbers","boardBackground"] as const;
export function setPreferenceOwner(owner:string|null){preferenceOwner=owner;if(typeof window!=="undefined")window.dispatchEvent(new Event(PREFERENCES_CHANGED));}

export function getPreferences(): KonturPreferences {
 if(typeof window==="undefined") return {...DEFAULT_PREFERENCES};
 try {
  let value = JSON.parse(localStorage.getItem(KEY) || "{}") as Partial<KonturPreferences>;
  if(!isLocalDesktop()&&preferenceOwner!==undefined){
   const shared=preferenceOwner?JSON.parse(localStorage.getItem(KEY+":owner:"+encodeURIComponent(preferenceOwner))||"{}"):{};
   const defaults=Object.fromEntries(SHARED_KEYS.map(key=>[key,DEFAULT_PREFERENCES[key]]));
   value={...value,...defaults,...Object.fromEntries(SHARED_KEYS.filter(key=>shared[key]!==undefined).map(key=>[key,shared[key]]))};
  }
  const next={...DEFAULT_PREFERENCES,...value};
  if(!["system","light","dark"].includes(next.theme)) next.theme="system";
  if(!["standard","large"].includes(next.textSize)) next.textSize="standard";
  if(!["comfortable","compact"].includes(next.density)) next.density="comfortable";
  if(!["interface","light","charcoal"].includes(next.editorTheme)) next.editorTheme="interface";
  if(![12,14,16].includes(next.editorFontSize)) next.editorFontSize=14;
  if(typeof next.editorWrap!=="boolean") next.editorWrap=DEFAULT_PREFERENCES.editorWrap;
  if(![2,4,8].includes(next.editorTabSize)) next.editorTabSize=4;
  if(typeof next.editorLineNumbers!=="boolean") next.editorLineNumbers=DEFAULT_PREFERENCES.editorLineNumbers;
  if(!Number.isFinite(next.outputHeight)||next.outputHeight<100||next.outputHeight>360) next.outputHeight=DEFAULT_PREFERENCES.outputHeight;
  if(!["blank","grid","dots"].includes(next.boardBackground)) next.boardBackground="blank";
  for(const key of ["boardColor","boardMarkerColor"] as const)if(typeof next[key]!=="string"||!/^#[0-9a-f]{6}$/i.test(next[key]))next[key]=DEFAULT_PREFERENCES[key];
  for(const key of ["boardWidth","boardMarkerWidth"] as const)if(!Number.isFinite(next[key])||next[key]<1||next[key]>24)next[key]=DEFAULT_PREFERENCES[key];
  for(const key of ["boardOpacity","boardMarkerOpacity"] as const)if(!Number.isFinite(next[key])||next[key]<.1||next[key]>1)next[key]=DEFAULT_PREFERENCES[key];
  for(const key of ["reducedMotion","autoHideNavigation","rightPanelVisible","restoreWorkspace"] as const)if(typeof next[key]!=="boolean")next[key]=DEFAULT_PREFERENCES[key];
  if(!Number.isFinite(next.drawingSmoothing)||next.drawingSmoothing<0||next.drawingSmoothing>1)next.drawingSmoothing=DEFAULT_PREFERENCES.drawingSmoothing;
  if(![32,64,128,256,512,1024].includes(next.fileCacheMb))next.fileCacheMb=DEFAULT_PREFERENCES.fileCacheMb;
  if(!["narrow","standard","wide"].includes(next.assistantWidth))next.assistantWidth="standard";
  if(!["hint","explain"].includes(next.hintMode))next.hintMode="hint";
  if(!Number.isInteger(next.defaultTaskType)||next.defaultTaskType<1||next.defaultTaskType>27) next.defaultTaskType=14;
  if(!["auto","luna","terra","sol"].includes(next.model)) next.model="auto";
  if(!["low","medium","high","xhigh"].includes(next.effort)) next.effort="medium";
  if(!["hint","hint_ladder","feynman","socratic","debug","review","compare","full_solution"].includes(next.tutorMode)) next.tutorMode="hint";
  return next;
 } catch {return {...DEFAULT_PREFERENCES};}
}
export function setPreferences(patch: Partial<KonturPreferences>): KonturPreferences {
 const next={...getPreferences(),...patch};
 if(typeof window==="undefined") return next;
 localStorage.setItem(KEY,JSON.stringify(next));
 if(preferenceOwner&&!isLocalDesktop())localStorage.setItem(KEY+":owner:"+encodeURIComponent(preferenceOwner),JSON.stringify(Object.fromEntries(SHARED_KEYS.map(key=>[key,next[key]]))));
 window.dispatchEvent(new Event(PREFERENCES_CHANGED));
 return next;
}

export type PreferenceSection = "interface" | "workspace" | "board" | "assistant";

const PREFERENCE_SECTION_KEYS: Record<PreferenceSection, readonly (keyof KonturPreferences)[]> = {
  interface: ["theme", "reducedMotion", "textSize", "density"],
  workspace: ["autoHideNavigation", "rightPanelVisible", "defaultTaskType", "restoreWorkspace", "editorTheme", "editorFontSize", "editorWrap", "editorTabSize", "editorLineNumbers", "outputHeight", "fileCacheMb"],
  board: ["boardBackground", "boardColor", "boardWidth", "boardOpacity", "boardMarkerColor", "boardMarkerWidth", "boardMarkerOpacity", "drawingSmoothing"],
  assistant: ["assistantWidth", "hintMode", "model", "effort", "tutorMode"],
};

export function resetPreferenceSection(section: PreferenceSection): KonturPreferences {
 const patch = Object.fromEntries(PREFERENCE_SECTION_KEYS[section].map((key) => [key, DEFAULT_PREFERENCES[key]])) as Partial<KonturPreferences>;
 return setPreferences(patch);
}

export function resetPreferences(section?: PreferenceSection): KonturPreferences {
 if (section) return resetPreferenceSection(section);
 return setPreferences({...DEFAULT_PREFERENCES});
}
export function usePreferences() {
 const [preferences,setState]=useState<KonturPreferences>(DEFAULT_PREFERENCES);
 useEffect(()=>{
  const update=()=>setState(getPreferences());
  update(); window.addEventListener(PREFERENCES_CHANGED,update); window.addEventListener("storage",update);
  return ()=>{window.removeEventListener(PREFERENCES_CHANGED,update);window.removeEventListener("storage",update);};
 },[]);
 return {preferences,updatePreferences:setPreferences};
}
