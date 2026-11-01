import { memo } from "react";

export const StatementContent = memo(function StatementContent({ source }: { source: string }) {
  return <div className="statement-body">{source}</div>;
});
