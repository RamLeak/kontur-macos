"use client";

import { useEffect, useState, type CSSProperties } from "react";
import { usePreferences } from "@/lib/kontur-preferences";
import styles from "./correct-answer-celebration.module.css";

const COLORS = ["#537ce9", "#51b894", "#e9b83e", "#ec9275", "#b08be0", "#65b9dd"];
const BURSTS = [
  { left: "25%", top: "37%", delay: 0 },
  { left: "73%", top: "32%", delay: 140 },
  { left: "49%", top: "24%", delay: 270 },
];

/** Decorative only: the answer's existing status announces the actual result. */
export function CorrectAnswerCelebration({ receipt }: { receipt: string | null }) {
  const { preferences } = usePreferences();
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    if (!receipt) return;
    setVisible(true);
    const timer = window.setTimeout(() => setVisible(false), 1800);
    return () => window.clearTimeout(timer);
  }, [receipt]);
  if (!visible) return null;
  return <div aria-hidden="true" className={styles.overlay} data-reduced-motion={preferences.reducedMotion || undefined} key={receipt}>
    <span className={styles.message}>Отличная работа!</span>
    {BURSTS.map((burst, index) => <span className={styles.burst} key={index} style={{ left: burst.left, top: burst.top }}>
      {Array.from({ length: 14 }, (_, particle) => {
        const angle = particle * Math.PI * 2 / 14;
        const distance = 64 + particle % 3 * 20;
        return <i className={styles.spark} key={particle} style={{
          "--spark-x": `${Math.cos(angle) * distance}px`,
          "--spark-y": `${Math.sin(angle) * distance + 34}px`,
          "--spark-color": COLORS[(particle + index) % COLORS.length],
          "--spark-delay": `${burst.delay}ms`,
        } as CSSProperties} />;
      })}
    </span>)}
  </div>;
}
