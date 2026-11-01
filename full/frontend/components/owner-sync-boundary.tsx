"use client";
import {useEffect, useRef, useState} from "react";
import {DEMO_MODE} from "@/lib/api";
import {getPreferences, PREFERENCES_CHANGED, setPreferences, type KonturPreferences} from "@/lib/kontur-preferences";
import {startOwnerSync, stopOwnerSync, useOwnerRecord, useOwnerSyncIdentity} from "@/lib/owner-sync";
import {SyncConflict} from "./sync-conflict";
import {isLocalDesktop} from "@/lib/local-fetch";

const sharedKeys = ["theme", "reducedMotion", "textSize", "defaultTaskType", "restoreWorkspace", "editorTheme",
  "editorFontSize", "editorWrap", "editorTabSize", "editorLineNumbers", "boardBackground"] as const;
function sharedPreferences() {
  const preferences = getPreferences();
  return Object.fromEntries(sharedKeys.map(key => [key, preferences[key]]));
}

export function OwnerSyncBoundary() {
  const ownerIdentity=useOwnerSyncIdentity();
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    if(!ownerIdentity)void startOwnerSync().catch(() => {if (active) setError("Не удалось включить синхронизацию между устройствами.");});
    return () => {active = false; if (!isLocalDesktop()) stopOwnerSync();};
  }, []);
  if (DEMO_MODE) return null;
  return <>{error && <p role="status" className="session-notice">{error}</p>}<SharedPreferencesSync/></>;
}

function SharedPreferencesSync() {
  const sync = useOwnerRecord({kind: "preferences"}, sharedPreferences());
  const applying = useRef(false);
  useEffect(() => {
    if (!sync.state.ready || sync.state.dirty || sync.state.conflict) return;
    if (sync.state.version === 0) return;
    const patch = Object.fromEntries(Object.entries(sync.state.value).filter(([key, value]) =>
      sharedKeys.includes(key as typeof sharedKeys[number]) && value !== null));
    applying.current = true;
    setPreferences(patch as Partial<KonturPreferences>);
    applying.current = false;
  }, [sync.state.ready, sync.state.value, sync.state.version, sync.state.dirty, sync.state.conflict]);
  useEffect(() => {
    const edit = () => {if (!applying.current) sync.edit(sharedPreferences());};
    window.addEventListener(PREFERENCES_CHANGED, edit);
    return () => window.removeEventListener(PREFERENCES_CHANGED, edit);
  }, [sync.edit]);
  return <SyncConflict state={sync.state} resolve={sync.resolve}/>;
}
