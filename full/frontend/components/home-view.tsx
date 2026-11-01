"use client";

import { useSearchParams } from "next/navigation";
import { ChatView } from "@/components/chat-view";
import { OverviewView } from "@/components/overview-view";

export function HomeView() {
  const params = useSearchParams();
  if (params.get("view") === "today") return <div style={{ overflow: "auto", height: "100%" }}><OverviewView /></div>;
  return <ChatView initialConversationId={params.get("conversation")} projectId={params.get("project")} />;
}
