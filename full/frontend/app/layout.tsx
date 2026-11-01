import type { Metadata } from "next";
import type { ReactNode } from "react";
import { LocalLifecycle } from "@/components/local-lifecycle";
import { LocalClientStateGate } from "@/components/local-client-state-gate";
import "./globals.css";
import "./kontur.css";

export const metadata: Metadata = {
  title: "Контур — подготовка к ЕГЭ по информатике",
  description: "Приватное рабочее пространство для решения задач ЕГЭ по информатике.",
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="ru">
      <body><LocalLifecycle /><LocalClientStateGate>{children}</LocalClientStateGate></body>
    </html>
  );
}
