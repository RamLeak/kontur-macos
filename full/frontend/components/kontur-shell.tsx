"use client";
import {createContext,useContext} from "react";
export type RightPanelMode="navigation"|"assistant";
export interface KonturShellState {
 navigationVisible:boolean;
 panelVisible:boolean;
 panelMode:RightPanelMode;
 panelHost:HTMLDivElement|null;
 toolbarHost:HTMLDivElement|null;
 focused:boolean;
 showPanel:(mode:RightPanelMode)=>void;
 hidePanel:()=>void;
 togglePanel:()=>void;
 toggleNavigation:()=>void;
 toggleFocus:()=>void;
 setWorkspaceActive:(active:boolean)=>void;
}
export const KonturShellContext=createContext<KonturShellState>({
 navigationVisible:true,panelVisible:false,panelMode:"navigation",panelHost:null,toolbarHost:null,focused:false,
 showPanel:()=>{},hidePanel:()=>{},togglePanel:()=>{},toggleNavigation:()=>{},toggleFocus:()=>{},setWorkspaceActive:()=>{}
});
export function useKonturShell(){return useContext(KonturShellContext);}
