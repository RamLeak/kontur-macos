import { AppShell } from "@/components/app-shell";
import { WorkspaceView } from "@/components/workspace-view";

export default function WorkspacePage() {
  return (
    <AppShell active="bank" workspace>
      <WorkspaceView />
    </AppShell>
  );
}
