import { AppShell } from "@/components/app-shell";
import { LearningView } from "@/components/learning-view";

export default function SkillsPage() {
  return (
    <AppShell active="skills">
      <LearningView initialTab="skills" />
    </AppShell>
  );
}
