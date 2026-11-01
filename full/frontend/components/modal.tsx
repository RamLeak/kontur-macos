"use client";
import type {ReactNode} from "react";
import {useEffect,useId,useRef,useState} from "react";
import {createPortal} from "react-dom";
import {Icon} from "./icon";
export function Modal({open,onClose,title,children}:{open:boolean;onClose:()=>void;title:string;children:ReactNode}){
 const [mounted,setMounted]=useState(false);const dialog=useRef<HTMLDivElement|null>(null);const closeRef=useRef(onClose);closeRef.current=onClose;
 const titleId=useId();useEffect(()=>setMounted(true),[]);
 useEffect(()=>{
  if(!mounted||!open)return;
  const previous=document.activeElement as HTMLElement|null;const app=document.querySelector<HTMLElement>(".app-shell");const wasInert=app?.inert??false;if(app)app.inert=true;
  const controls=()=>Array.from(dialog.current?.querySelectorAll<HTMLElement>("button:not(:disabled),a[href],input:not(:disabled),textarea:not(:disabled),select:not(:disabled),[tabindex='0']")??[]).filter(node=>node.getClientRects().length>0);
  const timer=requestAnimationFrame(()=>{controls()[0]?.focus();});
  const handle=(event:KeyboardEvent)=>{
   if(event.key==="Escape"){event.preventDefault();event.stopPropagation();closeRef.current();return;}
   if(event.key!=="Tab")return;
   const items=controls(),first=items[0],last=items.at(-1);
   if(!first){event.preventDefault();dialog.current?.focus();return;}
   if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}
   else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
  };
  document.addEventListener("keydown",handle,true);
  return()=>{cancelAnimationFrame(timer);document.removeEventListener("keydown",handle,true);if(app)app.inert=wasInert;if(previous?.isConnected)previous.focus();};
 },[mounted,open]);
 if(!mounted||!open)return null;
 return createPortal(<div className="kontur-modal-scrim" onPointerDown={event=>{if(event.target===event.currentTarget)closeRef.current();}}>
  <div ref={dialog} className="kontur-modal" role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1}>
   <header><h2 id={titleId}>{title}</h2><button className="icon-button" aria-label="Закрыть диалог" onClick={onClose} type="button"><Icon name="close" size={18}/></button></header>
   {children}
  </div>
 </div>,document.body);
}
