"use client";
import type {CSSProperties,ReactNode} from "react";
import {Suspense,useEffect,useState,useRef} from "react";
import Link from "next/link";
import {usePathname,useRouter,useSearchParams} from "next/navigation";
import {Icon} from "./icon";
import {OwnerSyncBoundary} from "./owner-sync-boundary";
import {stopOwnerSync,startOwnerSync,OWNER_SESSION_CHANGED} from "@/lib/owner-sync";
import {Sidebar} from "./sidebar";
import {KonturShellContext,type RightPanelMode} from "./kontur-shell";
import {bootstrapOwnerSession,logoutOwnerSession,DEMO_MODE} from "@/lib/api";
import {flushOpenDrafts} from "@/lib/draft-lifecycle";
import {readableError} from "@/lib/ui-copy";
import {usePreferences} from "@/lib/kontur-preferences";
import {useShortcuts} from "@/lib/kontur-shortcuts";
import {canToggleFullscreen,isPlainF11Event,toggleFullscreen} from "@/lib/fullscreen";
import {consumeNavigationEntry,hasActiveEscapeLayer,isEscapeOwnedTarget,isEscapeProtectedTarget,isPlainEscapeEvent,rememberNavigationEntry,resolveEscapeAction} from "@/lib/navigation";
import type {NavKey} from "@/lib/types";
type AuthState="checking"|"ready"|"error"|"signed-out";
const SIGNED_OUT_KEY="kontur:signed-out";
export function AppShell({children,active,workspace=false}:{children:ReactNode;active:NavKey;workspace?:boolean}){
 return <Suspense fallback={<main className="page-state"><p>Открываем рабочую область…</p></main>}>
  <AuthenticatedAppShell active={active} workspace={workspace}>{children}</AuthenticatedAppShell></Suspense>;
}
function AuthenticatedAppShell({children,active,workspace=false}:{children:ReactNode;active:NavKey;workspace?:boolean}){
  const {preferences,updatePreferences}=usePreferences();
 const pathname=usePathname();
 const searchParams=useSearchParams();
 const router=useRouter();
 const navigationPath=pathname+(searchParams.toString()?`?${searchParams.toString()}`:"");
 const [workspaceActive,setWorkspaceActive]=useState(workspace);
 useEffect(()=>setWorkspaceActive(workspace),[workspace,pathname]);
  const [navigationVisible,setNavigationVisible]=useState(false);
  const navigationVisibleRef=useRef(navigationVisible);
 const [panelVisible,setPanelVisible]=useState(false);
 const [panelMode,setPanelMode]=useState<RightPanelMode>("navigation");
 const [panelHost,setPanelHost]=useState<HTMLDivElement|null>(null);
 const panelRef=useRef<HTMLElement|null>(null);const previousFocusRef=useRef<HTMLElement|null>(null);
 const navigationRef=useRef<HTMLElement|null>(null);const previousNavigationFocusRef=useRef<HTMLElement|null>(null);
 const focusNavigationRef=useRef<boolean|null>(null);
  const fullscreenPendingRef=useRef(false);
 const [toolbarHost,setToolbarHost]=useState<HTMLDivElement|null>(null);
 const [compact,setCompact]=useState(false);
 const [focused,setFocused]=useState(false);
 const [signingOut,setSigningOut]=useState(false);
 const [signOutError,setSignOutError]=useState<string|null>(null);
  const [fullscreenNotice,setFullscreenNotice]=useState<string|null>(null);
 const [authError,setAuthError]=useState<string|null>(null);
 const [authState,setAuthState]=useState<AuthState>(DEMO_MODE?"ready":"checking");
 const [authAttempt,setAuthAttempt]=useState(0);
  function setNavigationState(next:boolean,persist=true){
   navigationVisibleRef.current=next;
   setNavigationVisible(next);
   if(persist&&!workspaceActive)updatePreferences({rightPanelVisible:next});
  }
  useEffect(()=>{
  const media=window.matchMedia("(max-width: 1100px)");
    const update=()=>{setCompact(media.matches);if(media.matches){setNavigationState(false,false);setPanelVisible(false);}else setNavigationState(workspace ? !preferences.autoHideNavigation : false,false);};
  update();media.addEventListener("change",update);return()=>media.removeEventListener("change",update);
  },[preferences.autoHideNavigation,workspace]);
 useEffect(()=>{
  const system=window.matchMedia("(prefers-color-scheme: dark)");
  const apply=()=>{
   document.documentElement.dataset.theme=preferences.theme==="system"?(system.matches?"dark":"light"):preferences.theme;
   document.documentElement.dataset.textSize=preferences.textSize;
   document.documentElement.dataset.reducedMotion=preferences.reducedMotion?"true":"false";
  };
  apply();system.addEventListener("change",apply);return()=>system.removeEventListener("change",apply);
 },[preferences.theme,preferences.textSize,preferences.reducedMotion]);
 useEffect(()=>{
  setPanelMode("navigation");setPanelVisible(false);setFocused(false);
   if(compact)setNavigationState(false,false);
   else setNavigationState(workspace ? !preferences.autoHideNavigation : false,false);
   },[pathname,compact,preferences.autoHideNavigation,workspace]);
 useEffect(()=>{
  if(DEMO_MODE)return;
  if(readSignedOutFlag()){setAuthState("signed-out");return;}
  let active=true;setAuthState("checking");setAuthError(null);
  void bootstrapOwnerSession().then(async()=>{await startOwnerSync().catch(()=>undefined);if(active)setAuthState("ready");})
  .catch(reason=>{if(active){setAuthError(readableError(reason,"Не удалось открыть личную сессию. Повторите вход."));setAuthState("error");}});
  return()=>{active=false;};
 },[authAttempt]);
 useEffect(()=>{
  const changed=()=>{setAuthState("signed-out");setSignOutError("Сессия владельца изменилась. Откройте личную сессию заново; черновики предыдущего владельца сохранены отдельно.");};
  window.addEventListener(OWNER_SESSION_CHANGED,changed);
  return()=>window.removeEventListener(OWNER_SESSION_CHANGED,changed);
 },[]);
  useEffect(()=>{
   let alive=true;
   const onFullscreenKeyDown=(event:KeyboardEvent)=>{
    if(!isPlainF11Event(event)||hasActiveEscapeLayer(document))return;
    const root=document.documentElement;
    if(!canToggleFullscreen(document,root))return;
    event.preventDefault();event.stopPropagation();
    if(fullscreenPendingRef.current)return;
    fullscreenPendingRef.current=true;setFullscreenNotice(null);
    const request=toggleFullscreen(document,root);
    if(!request){fullscreenPendingRef.current=false;return;}
    void request.catch(()=>{if(alive)setFullscreenNotice("Не удалось изменить полноэкранный режим.");})
      .finally(()=>{if(alive)fullscreenPendingRef.current=false;});
   };
   document.addEventListener("keydown",onFullscreenKeyDown,true);
   return()=>{alive=false;fullscreenPendingRef.current=false;document.removeEventListener("keydown",onFullscreenKeyDown,true);};
  },[]);
 function hidePanel(){setPanelVisible(false);}
  function showPanel(mode:RightPanelMode){
  setPanelMode(mode);setFocused(false);
   if(mode==="navigation"){setPanelVisible(false);setNavigationState(true);}
  else setPanelVisible(true);
 }
  function hideNavigation(){setNavigationState(false);}
  function toggleNavigation(){setNavigationState(!navigationVisibleRef.current);}
  function togglePanel(){toggleNavigation();}
 function toggleFocus(){
  setFocused(value=>{
   if(value){setNavigationState(focusNavigationRef.current ?? navigationVisible,false);focusNavigationRef.current=null;return false;}
   focusNavigationRef.current=navigationVisible;setNavigationState(false,false);return true;
  });
 }
 const visible=panelVisible&&!focused&&authState==="ready";
 const navigationOpen=navigationVisible&&authState==="ready";
 const navigationRailVisible=authState==="ready"&&!workspaceActive;
 const navigationLayerVisible=navigationRailVisible||navigationOpen;
 useEffect(()=>{rememberNavigationEntry(navigationPath);},[navigationPath]);
 useEffect(()=>{
  const onEscape=(event:KeyboardEvent)=>{
    // Keep the shell state tuple explicit: compact,navigationOpen,panelVisible,focused.
   const action=resolveEscapeAction({
    plainEscape:isPlainEscapeEvent(event),
    defaultPrevented:event.defaultPrevented,
    fullscreen:Boolean(document.fullscreenElement),
    activeEscapeLayer:hasActiveEscapeLayer(document),
    ownedTarget:isEscapeOwnedTarget(event.target),
    protectedTarget:isEscapeProtectedTarget(event.target),
     compact:compact||navigationRailVisible,navigationOpen,panelVisible,focused,
   });
   if(action==="ignore")return;
   if(action==="close-drawer"){event.preventDefault();event.stopPropagation();hideNavigation();return;}
   if(action==="close-panel"){event.preventDefault();event.stopPropagation();hidePanel();return;}
   if(action==="close-focus"){event.preventDefault();event.stopPropagation();toggleFocus();return;}
   const target=consumeNavigationEntry({pathname,search:searchParams.toString()?`?${searchParams.toString()}`:""});
   if(!target||target===navigationPath)return;
   event.preventDefault();event.stopPropagation();void router.push(target);
  };
  document.addEventListener("keydown",onEscape,true);
  return()=>document.removeEventListener("keydown",onEscape,true);
 },[compact,focused,navigationPath,navigationOpen,navigationRailVisible,panelVisible,pathname,router,searchParams]);
 useShortcuts("global",authState==="ready"?{"panel.toggle":togglePanel}:{});
 async function signOut(){
  if(signingOut)return;setSigningOut(true);setSignOutError(null);
  try{await flushOpenDrafts();await logoutOwnerSession();stopOwnerSync();try{sessionStorage.setItem(SIGNED_OUT_KEY,"true");}catch{}
   setAuthState("signed-out");setPanelVisible(false);
  }catch{setSignOutError("Не удалось сохранить работу или завершить сессию. Попробуйте ещё раз.");}
  finally{setSigningOut(false);}
 }
 function signIn(){try{sessionStorage.removeItem(SIGNED_OUT_KEY);}catch{}if(DEMO_MODE)setAuthState("ready");else setAuthAttempt(n=>n+1);}
 useEffect(()=>{
  if(!compact||!navigationOpen)return;
  const panel=navigationRef.current;if(!panel)return;
  if(!panel.contains(document.activeElement))previousNavigationFocusRef.current=document.activeElement as HTMLElement|null;
  const controls=()=>Array.from(panel.querySelectorAll<HTMLElement>("a[href],button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),[tabindex='0']")).filter(item=>!item.closest("[hidden],[inert]")&&item.getClientRects().length>0);
  const focusTimer=requestAnimationFrame(()=>controls()[0]?.focus());
  const key=(event:KeyboardEvent)=>{
   if(event.key==="Escape"){if(event.defaultPrevented||hasActiveEscapeLayer(document))return;event.preventDefault();hideNavigation();return;}
   if(event.key!=="Tab")return;
   const items=controls(),first=items[0],last=items.at(-1);
   if(!first){event.preventDefault();panel.focus();return;}
   if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}
   else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
  };
  document.addEventListener("keydown",key);
  return()=>{cancelAnimationFrame(focusTimer);document.removeEventListener("keydown",key);previousNavigationFocusRef.current?.focus();};
 },[compact,navigationOpen]);
 useEffect(()=>{
  if(!compact||!visible||workspaceActive)return;
  const panel=panelRef.current;if(!panel)return;
  if(!panel.contains(document.activeElement))previousFocusRef.current=document.activeElement as HTMLElement|null;
  const controls=()=>Array.from(panel.querySelectorAll<HTMLElement>("a[href],button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),[tabindex='0']")).filter(item=>!item.closest("[hidden],[inert]")&&item.getClientRects().length>0);
  const focusTimer=requestAnimationFrame(()=>controls()[0]?.focus());
  const key=(event:KeyboardEvent)=>{if(event.key==="Escape"){if(event.defaultPrevented)return;event.preventDefault();hidePanel();return;}if(event.key!=="Tab")return;const items=controls(),first=items[0],last=items.at(-1);if(!first){event.preventDefault();panel.focus();return;}if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}};
  document.addEventListener("keydown",key);
  return()=>{cancelAnimationFrame(focusTimer);document.removeEventListener("keydown",key);previousFocusRef.current?.focus();};
 },[compact,visible,workspaceActive,panelMode]);
 const assistantWidth = preferences.assistantWidth === "narrow" ? "300px" : preferences.assistantWidth === "wide" ? "440px" : "360px";
 return <KonturShellContext.Provider value={{navigationVisible:navigationOpen,panelVisible:visible,panelMode,panelHost,toolbarHost,focused,showPanel,hidePanel,togglePanel,toggleNavigation,toggleFocus,setWorkspaceActive}}>
  <div inert={signingOut} aria-busy={signingOut} data-density={preferences.density} style={{"--assistant-width":assistantWidth} as CSSProperties} className={"app-shell kontur-app-shell"+(workspaceActive?" is-workspace":"")+(navigationOpen&&!navigationRailVisible?" has-navigation":"")+(navigationRailVisible?" has-navigation-rail":"")+(visible&&!workspaceActive?" has-assistant-panel":"")+(compact?" is-compact":"")+(focused?" is-focus":"")}>
   <aside ref={navigationRef} id="kontur-navigation" className={"kontur-navigation-rail"+(navigationLayerVisible?" is-visible":"")+(navigationRailVisible?" is-rail":"")} aria-label="Навигация" aria-hidden={!navigationLayerVisible} inert={!navigationLayerVisible}>
    {authState==="ready"&&<Sidebar active={active} open={navigationOpen} collapsed={false} rail={navigationRailVisible} onToggle={toggleNavigation} onClose={hideNavigation} onSignOut={signOut} signingOut={signingOut}/>}
  </aside>
  <section className="app-main" inert={compact&&navigationOpen}>
   {authState==="ready"&&<OwnerSyncBoundary/>}
   {authState==="ready"&&<header className="kontur-topbar">
    <Link href="/tasks" className="kontur-brand" aria-label="Контур — банк заданий"><span className="kontur-brand-mark">К</span><span>Контур</span></Link>
    <div ref={setToolbarHost} className="kontur-toolbar-slot"/><div className="kontur-topbar-spacer"/>
    <button className="icon-button kontur-panel-toggle" type="button" aria-label={navigationOpen?"Скрыть навигацию":"Показать навигацию"} title={navigationOpen?"Скрыть навигацию":"Показать навигацию"} aria-expanded={navigationOpen} aria-controls="kontur-navigation" onClick={toggleNavigation}><Icon name="panel" size={20}/></button>
   </header>}
   {DEMO_MODE&&<div className="demo-banner" role="status">Демонстрационный набор · не задания КЕГЭ</div>}
   {signOutError&&<div className="session-notice" role="alert">{signOutError}<button type="button" className="icon-button" aria-label="Закрыть уведомление" onClick={()=>setSignOutError(null)}><Icon name="close" size={18}/></button></div>}
    {fullscreenNotice&&<div className="session-notice" role="status">{fullscreenNotice}<button type="button" className="icon-button" aria-label="Закрыть уведомление о полноэкранном режиме" onClick={()=>setFullscreenNotice(null)}><Icon name="close" size={18}/></button></div>}
   {authState==="ready"?children:<main className="page-state" aria-live="polite">
    <div className="state-icon"><Icon name="lock" size={22}/></div>
    <h1>{authState==="checking"?"Открываем рабочую область":authState==="signed-out"?"Вы вышли из рабочей области":"Не удалось войти"}</h1>
    <p>{authState==="checking"?"Подготовка скоро продолжится.":authState==="signed-out"?"Вернитесь к личной рабочей сессии, чтобы продолжить.":authError}</p>
    {authState!=="checking"&&<button type="button" className="primary-button" onClick={signIn}>{authState==="signed-out"?"Вернуться к подготовке":"Повторить вход"}</button>}
   </main>}
  </section>
  {compact&&navigationOpen&&<button className="kontur-panel-scrim" type="button" aria-label="Закрыть навигацию" onClick={hideNavigation}/>} 
  <aside ref={panelRef} role={compact&&visible&&!workspaceActive?"dialog":undefined} aria-modal={compact&&visible&&!workspaceActive?true:undefined} tabIndex={-1} id="kontur-right-panel" className={"kontur-right-panel"+(visible&&!workspaceActive?" is-visible":"")} aria-label="Помощник задания" aria-hidden={!visible||workspaceActive} inert={!visible||workspaceActive}>
   <div ref={setPanelHost} className="kontur-assistant-host" />
  </aside>
 </div></KonturShellContext.Provider>;
}
function readSignedOutFlag(){try{return sessionStorage.getItem(SIGNED_OUT_KEY)==="true";}catch{return false;}}
