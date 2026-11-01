import { AppShell } from "@/components/app-shell";
import { BookmarksView } from "@/components/bookmarks-view";

export default function BookmarksPage() {
  return (
    <AppShell active="bookmarks">
      <BookmarksView />
    </AppShell>
  );
}
