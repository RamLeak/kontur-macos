import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";
import "./kontur.css";
import "./local.css";

export const metadata: Metadata = {
  title: "Контур Локальный",
  description: "Локальная рабочая область для подготовки к ЕГЭ по информатике.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return <html lang="ru" data-theme="light"><body>{children}</body></html>;
}
