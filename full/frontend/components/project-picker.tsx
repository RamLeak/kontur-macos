"use client";

import { Icon } from "@/components/icon";
import type { Project } from "@/lib/types";
import styles from "./project-picker.module.css";

export function ProjectPicker({
  projects,
  value,
  onChange,
}: {
  projects: Project[];
  value: string | null;
  onChange: (projectId: string | null) => void;
}) {
  return (
    <div className={styles.picker}>
      <Icon aria-hidden="true" className={styles.icon} name="folder" size={16} />
      <label className={styles.label} htmlFor="new-chat-project">Папка чата</label>
      <select
        aria-label="Папка чата"
        className={`${styles.select} composer-select`}
        id="new-chat-project"
        onChange={(event) => onChange(event.target.value || null)}
        value={value ?? ""}
      >
        <option value="">Без проекта</option>
        {projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
      </select>
    </div>
  );
}
