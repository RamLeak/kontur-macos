import { AppShell } from "@/components/app-shell";
import { LearningView } from "@/components/learning-view";

export default function ErrorsPage() {
  return (
    <AppShell active="errors">
      <LearningView initialTab="errors" />
    </AppShell>
  );
}
