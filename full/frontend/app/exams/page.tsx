import { AppShell } from "@/components/app-shell";
import { ExamsView } from "@/components/exams-view";

export default function ExamsPage() {
  return (
    <AppShell active="exams">
      <ExamsView />
    </AppShell>
  );
}
