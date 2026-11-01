import { AppShell } from "@/components/app-shell";
import { TaskBankView } from "@/components/task-bank-view";

export default function TasksPage() {
  return (
    <AppShell active="bank">
      <TaskBankView />
    </AppShell>
  );
}
