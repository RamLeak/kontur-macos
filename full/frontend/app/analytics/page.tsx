import { AppShell } from "@/components/app-shell";
import { AnalyticsView } from "@/components/analytics-view";

export default function AnalyticsPage() {
  return (
    <AppShell active="analytics">
      <AnalyticsView />
    </AppShell>
  );
}
