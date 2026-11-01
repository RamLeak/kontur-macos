"use client";

import Link from "next/link";
import { FormEvent, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Icon } from "@/components/icon";
import { ArchivedChats } from "@/components/archived-chats";
import { SourceSettings } from "@/components/source-settings";
import { LocalDataSettings } from "@/components/local-data-settings";
import { LocalAISettings } from "@/components/local-ai-settings";
import { ShortcutsSettings } from "@/components/shortcuts-settings";
import { DEMO_MODE, createCodeSample, getCodeSamples, updateCodeSample } from "@/lib/api";
import { MODEL_OPTIONS } from "@/lib/model-policy";
import { resetPreferenceSection, usePreferences } from "@/lib/kontur-preferences";
import { clearDeviceFileCache } from "@/lib/device-files";
import { readableError } from "@/lib/ui-copy";
import { filterSettingsSections } from "@/lib/settings-search";
import type { CodeSample, Effort, ModelId, TutorMode } from "@/lib/types";
import styles from "./settings.module.css";

type SettingsSection = "interface" | "workspace" | "board" | "assistant" | "shortcuts" | "profile" | "samples" | "advanced" | "archive" | "data";

const sectionItems: Array<{
  id: SettingsSection;
  label: string;
  description: string;
  icon: "target" | "spark" | "code" | "settings" | "chat" | "panel" | "board";
  keywords: string[];
}> = [
  { id: "interface", label: "Интерфейс", description: "Тема и доступность", icon: "settings", keywords: ["тема размер текста плотность движение"] },
  { id: "workspace", label: "Рабочее пространство", description: "Возврат и редактор", icon: "panel", keywords: ["черновик навигация код шрифт строки кэш"] },
  { id: "board", label: "Доска", description: "Инструменты и фон", icon: "board", keywords: ["перо маркер цвет толщина сетка точки"] },
  { id: "profile", label: "Профиль", description: "Владелец рабочей области", icon: "target", keywords: ["владелец рабочая область источник режим"] },
  { id: "assistant", label: "Помощник", description: "Как проходит разбор", icon: "spark", keywords: ["модель codex подсказка подключение api ключ"] },
  { id: "shortcuts", label: "Горячие клавиши", description: "Команды и профили", icon: "settings", keywords: ["команды windows macos сочетания клавиш"] },
  { id: "samples", label: "Образцы кода", description: "Ваши шаблоны Python", icon: "code", keywords: ["python шаблон код теги задания"] },
  { id: "advanced", label: "Дополнительно", description: "Подключения и источник", icon: "settings", keywords: ["кегэ сервисы банк запуск подключение"] },
  { id: "data", label: "Данные", description: "Импорт .kontur и резервная копия", icon: "settings", keywords: ["импорт экспорт резервная копия хранилище"] },
  { id: "archive", label: "Архив чатов", description: "Сохранённые разговоры", icon: "chat", keywords: ["история разбор восстановить разговор"] },
];

export function SettingsView() {
  return (
    <Suspense fallback={<main className="page-state"><p>Загружаем настройки…</p></main>}>
      <SettingsContent />
    </Suspense>
  );
}

function SettingsContent() {
  const searchParams = useSearchParams();
  const [section, setSection] = useState<SettingsSection>("interface");
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const [sectionQuery, setSectionQuery] = useState("");
  const [samples, setSamples] = useState<CodeSample[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState({ title: "", content: "", tags: "", taskLines: "", usageNote: "" });

  useEffect(() => {
    let cancelled = false;
    getCodeSamples()
      .then((nextSamples) => {
        if (cancelled) return;
        setSamples(nextSamples);
        setLoading(false);
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setError(readableError(reason, "Не удалось загрузить образцы кода. Повторите попытку."));
          setLoading(false);
        }
      });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    setSection(getSection(searchParams.get("section")));
  }, [searchParams]);

  useEffect(() => {
    const handleLocationChange = () => {
      const url = new URL(window.location.href);
      setSection(getSection(url.searchParams.get("section")));
    };
    window.addEventListener("popstate", handleLocationChange);
    window.addEventListener("hashchange", handleLocationChange);
    return () => {
      window.removeEventListener("popstate", handleLocationChange);
      window.removeEventListener("hashchange", handleLocationChange);
    };
  }, [searchParams]);

  function selectSection(nextSection: SettingsSection) {
    setSection(nextSection);
    setNotice(null);
    setError(null);
    const url = new URL(window.location.href);
    url.pathname = "/settings";
    url.searchParams.set("section", nextSection);
    window.history.replaceState(null, "", url.pathname + "?" + url.searchParams.toString());
  }

  function handleTabKeyDown(event: React.KeyboardEvent<HTMLButtonElement>, index: number, items: typeof sectionItems) {
    let nextIndex: number | null = null;
    if (!items.length) return;
    if (event.key === "ArrowDown" || event.key === "ArrowRight") nextIndex = (index + 1) % items.length;
    if (event.key === "ArrowUp" || event.key === "ArrowLeft") nextIndex = (index - 1 + items.length) % items.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = items.length - 1;
    if (nextIndex == null) return;
    event.preventDefault();
    const nextSection = items[nextIndex].id;
    selectSection(nextSection);
    tabRefs.current[nextIndex]?.focus();
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!form.title.trim() || !form.content.trim() || saving) return;
    setSaving(true);
    setNotice(null);
    setError(null);
    try {
      const sample = await createCodeSample({
        title: form.title.trim(),
        language: "python",
        content: form.content,
        task_lines: parseLines(form.taskLines),
        tags: form.tags.split(",").map((tag) => tag.trim()).filter(Boolean),
        usage_note: form.usageNote.trim(),
      });
      setSamples((current) => [sample, ...current]);
      setForm({ title: "", content: "", tags: "", taskLines: "", usageNote: "" });
      setNotice(DEMO_MODE ? "Образец добавлен в локальный preview." : "Образец сохранён.");
    } catch (reason: unknown) {
      setError(readableError(reason, "Не удалось сохранить образец. Проверьте поля и повторите попытку."));
    } finally {
      setSaving(false);
    }
  }

  async function toggle(sample: CodeSample) {
    setError(null);
    try {
      const updated = await updateCodeSample(sample.id, { enabled: !sample.enabled });
      setSamples((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (reason: unknown) {
      setError(readableError(reason, "Не удалось изменить образец. Повторите попытку."));
    }
  }

  const activeItem = sectionItems.find((item) => item.id === section) ?? sectionItems[0];
  const visibleSectionItems = useMemo(() => filterSettingsSections(sectionItems, sectionQuery), [sectionQuery]);
  const activeSectionVisible = visibleSectionItems.some((item) => item.id === section);

  return (
    <main className={`page-content settings-page ${styles.page}`}>
      <div className={styles.settingsShell}>
        <aside className={styles.settingsSidebar} aria-label="Настройки">
          <div className={styles.sidebarHeader}>
            <h1>Настройки</h1>
            <Link aria-label="Закрыть настройки" title="Закрыть настройки · Esc" className={styles.closeLink} href={getReturnPath(searchParams.get("returnTo"))}><Icon name="close" size={18} /></Link>
          </div>
          <label className={styles.sectionSearch}>
            <Icon name="search" size={17} />
            <span className="sr-only">Поиск разделов настроек</span>
            <input aria-label="Поиск разделов настроек" onChange={(event) => setSectionQuery(event.target.value)} placeholder="Поиск" type="search" value={sectionQuery} />
          </label>
          {!visibleSectionItems.length ? <p className={styles.searchEmpty}>Ничего не найдено. Попробуйте другой запрос.</p> : null}
          <nav className={styles.tabs} aria-label="Разделы настроек" aria-orientation="vertical" role="tablist">
            {visibleSectionItems.map((item, index) => (
              <button
                aria-controls={`settings-panel-${item.id}`}
                aria-selected={item.id === section}
                className={`${styles.tab} ${item.id === section ? styles.tabActive : ""}`}
                id={`settings-tab-${item.id}`}
                key={item.id}
                onKeyDown={(event) => handleTabKeyDown(event, index, visibleSectionItems)}
                onClick={() => selectSection(item.id)}
                ref={(element) => { tabRefs.current[index] = element; }}
                role="tab"
                tabIndex={item.id === section || (!activeSectionVisible && index === 0) ? 0 : -1}
                type="button"
              >
                <span className={styles.tabIcon}><Icon name={item.icon} size={16} /></span>
                <span>{item.label}</span>
              </button>
            ))}
          </nav>
        </aside>

        <section className={styles.mainPane}>
          {DEMO_MODE ? <div className={styles.demoNote} role="note"><Icon name="spark" size={15} /><span>Локальный preview: изменения образцов остаются в браузере и не отправляются в Codex.</span></div> : null}
          {notice ? <div className={styles.feedbackSuccess} role="status"><Icon name="check" size={15} />{notice}</div> : null}
          {error ? <div className={styles.feedbackError} role="alert"><Icon name="warning" size={15} />{error}</div> : null}
          <div
            aria-label={activeItem.label}
            aria-labelledby={activeSectionVisible ? `settings-tab-${section}` : undefined}
            className={styles.content}
            id={`settings-panel-${section}`}
            role="tabpanel"
            tabIndex={-1}
          >
            {section === "interface" ? <InterfaceSection /> : null}
            {section === "workspace" ? <WorkspaceSection /> : null}
            {section === "board" ? <BoardSection /> : null}
            {section === "profile" ? <ProfileSection /> : null}
            {section === "assistant" ? <AssistantSection /> : null}
            {section === "shortcuts" ? <ShortcutsSettings /> : null}
            {section === "samples" ? <SamplesSection form={form} loading={loading} saving={saving} samples={samples} setForm={setForm} onSubmit={submit} onToggle={toggle} /> : null}
            {section === "advanced" ? <SourceSettings onImport={() => selectSection("data")} /> : null}
            {section === "data" ? <LocalDataSettings /> : null}
            {section === "archive" ? <ArchivedChats /> : null}
          </div>
        </section>
      </div>
    </main>
  );
}

function ProfileSection() {
  return (
    <section className={styles.panel} aria-labelledby="profile-title">
      <div className={styles.panelHeading}>
        <div>
          <span className="eyebrow">Профиль владельца</span>
          <h2 id="profile-title">Профиль</h2>
          <p>Личный профиль для вашей рабочей области. Здесь не требуется отдельная регистрация.</p>
        </div>
      </div>
      <div className={styles.profileCard}>
        <span className={styles.largeAvatar} aria-hidden="true">Д</span>
        <div className={styles.profileCopy}>
          <strong>Даниил</strong>
          <span>Владелец личной рабочей области</span>
        </div>
        <span className={styles.ownerBadge}>Владелец</span>
      </div>
      <div className={styles.infoGrid}>
        <div className={styles.infoItem}><small>Рабочая область</small><strong>ЕГЭ 2027</strong></div>
        <div className={styles.infoItem}><small>Источник заданий</small><strong>КЕГЭ</strong></div>
        <div className={styles.infoItem}><small>Режим</small><strong>Личная подготовка</strong></div>
        <div className={styles.infoItem}><small>Данные</small><strong>Хранятся в рабочей области</strong></div>
      </div>
      <div className={styles.callout}><Icon name="lock" size={15} /><span>Профиль не публикуется и не меняет доступ к заданиям. Защищённые действия остаются доступны только владельцу.</span></div>
    </section>
  );
}

function AssistantSection() {
  const { preferences, updatePreferences } = usePreferences();
  const tutorOptions: Array<{ value: TutorMode; label: string }> = [
    { value: "hint", label: "Подсказка" },
    { value: "hint_ladder", label: "Подсказка по шагам" },
    { value: "feynman", label: "Объяснить просто" },
    { value: "socratic", label: "Наводящий вопрос" },
    { value: "debug", label: "Найти ошибку" },
    { value: "review", label: "Разбор" },
    { value: "compare", label: "Сравнение" },
    { value: "full_solution", label: "Полное решение" },
  ];
  const selectedModelOptions = MODEL_OPTIONS.filter((option) => option.model === preferences.model);
  function resetAssistant() {
    resetPreferenceSection("assistant");
  }
  return (
    <section className={styles.panel} aria-labelledby="assistant-title">
      <div className={styles.panelHeading}>
        <div>
          <span className="eyebrow">Разбор задач</span>
          <h2 id="assistant-title">Помощник</h2>
          <p>Выберите удобный способ разбора задач. Настройки применятся к следующим запросам.</p>
        </div>
      </div>
      <div className={styles.settingList}>
        <SettingRow label="Ширина помощника" description="Определяет ширину панели помощника на страницах каталога и чатов.">
          <SegmentedControl ariaLabel="Ширина помощника" value={preferences.assistantWidth} options={[{ value: "narrow", label: "Узкая" }, { value: "standard", label: "Стандартная" }, { value: "wide", label: "Широкая" }]} onChange={(value) => updatePreferences({ assistantWidth: value as "narrow" | "standard" | "wide" })} />
        </SettingRow>
        <SettingRow label="Формат помощи" description="Начальный режим запроса в поле помощника.">
          <select className={styles.settingSelect} onChange={(event) => updatePreferences({ tutorMode: event.target.value as TutorMode })} value={preferences.tutorMode}>
            {tutorOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
          </select>
        </SettingRow>
        <SettingRow label="Модель Codex" description="Можно доверить выбор помощнику или выбрать модель самостоятельно.">
          <select className={styles.settingSelect} onChange={(event) => updatePreferences({ model: event.target.value as ModelId })} value={preferences.model}>
            <option value="auto">Авто</option><option value="luna">Luna</option><option value="terra">Terra</option><option value="sol">Sol</option>
          </select>
        </SettingRow>
        <SettingRow label="Мощность ответа" description="Выберите уровень только для ручной модели.">
          <select className={styles.settingSelect} disabled={preferences.model === "auto"} onChange={(event) => updatePreferences({ effort: event.target.value as Effort })} value={preferences.effort}>
            {preferences.model === "auto" ? <option value="medium">Авто</option> : null}
            {selectedModelOptions.map((option) => <option key={option.effort} value={option.effort}>{option.effort === "low" ? "Низкий" : option.effort === "medium" ? "Средний" : option.effort === "high" ? "Высокий" : "Максимальный"}</option>)}
          </select>
        </SettingRow>
      </div>
      <LocalAISettings />
      <div className={styles.sectionActions}><button className={styles.resetButton} type="button" onClick={resetAssistant}>Сбросить раздел</button></div>
      <div className={styles.assistantList}>
        <div className={styles.assistantRow}><Icon name="spark" size={17} /><div className={styles.assistantCopy}><strong>Выбрано</strong><span>Настройки применяются при следующем запросе</span></div><span className={styles.assistantValue}>{preferences.model === "auto" ? "Авто" : preferences.model + " · " + preferences.effort}</span></div>
        <div className={styles.assistantRow}><Icon name="code" size={17} /><div className={styles.assistantCopy}><strong>Образцы кода</strong><span>Подключаются только после явного выбора</span></div><span className={styles.assistantValue}>Вручную</span></div>
        <div className={styles.assistantRow}><Icon name="lock" size={17} /><div className={styles.assistantCopy}><strong>Экзаменационный режим</strong><span>Подсказки и образцы недоступны во время экзамена</span></div><span className={styles.assistantValue}>Без подсказок</span></div>
      </div>
      <div className={styles.assistantAction}><Link className="primary-button" href="/tasks">Выбрать задание <Icon name="chevron-right" size={16} /></Link></div>
    </section>
  );
}

function InterfaceSection() {
  const { preferences, updatePreferences } = usePreferences();
  return (
    <section className={styles.panel} aria-labelledby="interface-title">
      <div className={styles.panelHeading}><div><span className="eyebrow">Внешний вид</span><h2 id="interface-title">Интерфейс</h2><p>Настройки применяются к рабочей области и сохраняются в браузере.</p></div></div>
      <div className={styles.settingList}>
        <SettingRow label="Тема" description="Системная тема учитывает настройку устройства.">
          <SegmentedControl ariaLabel="Тема" value={preferences.theme} options={[{ value: "system", label: "Как в системе" }, { value: "light", label: "Светлая" }, { value: "dark", label: "Тёмная" }]} onChange={(value) => updatePreferences({ theme: value as "system" | "light" | "dark" })} />
        </SettingRow>
        <SettingRow label="Размер текста" description="Увеличенный размер помогает при длительной работе.">
          <SegmentedControl ariaLabel="Размер текста" value={preferences.textSize} options={[{ value: "standard", label: "Стандартный" }, { value: "large", label: "Увеличенный" }]} onChange={(value) => updatePreferences({ textSize: value as "standard" | "large" })} />
        </SettingRow>
        <SettingRow label="Плотность интерфейса" description="Компактный режим оставляет больше места для содержимого.">
          <SegmentedControl ariaLabel="Плотность интерфейса" value={preferences.density} options={[{ value: "comfortable", label: "Комфортная" }, { value: "compact", label: "Компактная" }]} onChange={(value) => updatePreferences({ density: value as "comfortable" | "compact" })} />
        </SettingRow>
        <SettingRow label="Уменьшение движения" description="Отключает перемещение и масштабирование интерфейса.">
          <label className={styles.switchControl}><span className="sr-only">Уменьшение движения</span><input checked={preferences.reducedMotion} onChange={(event) => updatePreferences({ reducedMotion: event.target.checked })} type="checkbox" /><span className={styles.switchTrack} /></label>
        </SettingRow>
      </div>
      <div className={styles.sectionActions}><button className={styles.resetButton} type="button" onClick={() => resetPreferenceSection("interface")}>Сбросить раздел</button></div>
    </section>
  );
}

function WorkspaceSection() {
  const { preferences, updatePreferences } = usePreferences();
  const [cacheBusy, setCacheBusy] = useState(false);
  const [cacheNotice, setCacheNotice] = useState<string | null>(null);
  const [cacheError, setCacheError] = useState<string | null>(null);
  async function clearCache() {
    if (cacheBusy) return;
    setCacheBusy(true); setCacheNotice(null); setCacheError(null);
    try { await clearDeviceFileCache(); setCacheNotice("Кэш файлов очищен."); }
    catch (reason: unknown) { setCacheError(readableError(reason, "Не удалось полностью очистить кэш файлов.")); }
    finally { setCacheBusy(false); }
  }
  return (
    <section className={styles.panel} aria-labelledby="workspace-settings-title">
      <div className={styles.panelHeading}><div><span className="eyebrow">Рабочее место</span><h2 id="workspace-settings-title">Рабочее пространство</h2><p>Выберите, как открываются задачи и редактор. Черновики при этом не меняются.</p></div></div>
      <div className={styles.settingList}>
        <SettingRow label="Восстанавливать черновик" description="Повторное открытие задания вернёт вашу работу и выбранный инструмент.">
          <label className={styles.switchControl}><span className="sr-only">Возвращать на место работы</span><input checked={preferences.restoreWorkspace} onChange={(event) => updatePreferences({ restoreWorkspace: event.target.checked })} type="checkbox" /><span className={styles.switchTrack} /></label>
        </SettingRow>
        <SettingRow label="Автоскрытие навигации" description="На странице решения навигация скрывается, чтобы освободить место для кода и условия.">
          <label className={styles.switchControl}><span className="sr-only">Автоскрытие навигации</span><input checked={preferences.autoHideNavigation} onChange={(event) => updatePreferences({ autoHideNavigation: event.target.checked })} type="checkbox" /><span className={styles.switchTrack} /></label>
        </SettingRow>
        <SettingRow label="Тип при открытии банка" description="Если вы ещё не выбирали тип задания, банк откроется здесь.">
          <select className={styles.settingSelect} value={preferences.defaultTaskType} onChange={(event) => updatePreferences({ defaultTaskType: Number(event.target.value) })}>{Array.from({length:27},(_,index)=><option key={index+1} value={index+1}>Тип {index+1}</option>)}</select>
        </SettingRow>
        <SettingRow label="Тема редактора" description="Цвета редактора кода не влияют на тему оболочки.">
          <select className={styles.settingSelect} value={preferences.editorTheme} onChange={(event) => updatePreferences({ editorTheme: event.target.value as "interface" | "light" | "charcoal" })}><option value="interface">Как в интерфейсе</option><option value="light">Светлая</option><option value="charcoal">Угольная</option></select>
        </SettingRow>
        <SettingRow label="Размер шрифта редактора" description="Размер текста в редакторе кода.">
          <SegmentedControl ariaLabel="Размер шрифта редактора" value={String(preferences.editorFontSize)} options={[{ value: "12", label: "12" }, { value: "14", label: "14" }, { value: "16", label: "16" }]} onChange={(value) => updatePreferences({ editorFontSize: Number(value) as 12 | 14 | 16 })} />
        </SettingRow>
        <SettingRow label="Перенос строк" description="Переносить длинные строки внутри редактора.">
          <label className={styles.switchControl}><span className="sr-only">Перенос строк</span><input checked={preferences.editorWrap} onChange={(event) => updatePreferences({ editorWrap: event.target.checked })} type="checkbox" /><span className={styles.switchTrack} /></label>
        </SettingRow>
        <SettingRow label="Размер отступа Tab" description="Число пробелов при нажатии Tab в редакторе.">
          <select className={styles.settingSelect} aria-label="Размер отступа Tab" value={preferences.editorTabSize} onChange={(event) => updatePreferences({ editorTabSize: Number(event.target.value) as 2 | 4 | 8 })}><option value={2}>2 пробела</option><option value={4}>4 пробела</option><option value={8}>8 пробелов</option></select>
        </SettingRow>
        <SettingRow label="Номера строк" description="Показывать номера строк рядом с кодом.">
          <label className={styles.switchControl}><span className="sr-only">Номера строк</span><input checked={preferences.editorLineNumbers} onChange={(event) => updatePreferences({ editorLineNumbers: event.target.checked })} type="checkbox" /><span className={styles.switchTrack} /></label>
        </SettingRow>
        <SettingRow label="Высота вывода" description="Размер панели вывода после запуска кода.">
          <input aria-label="Высота вывода" className={styles.rangeInput} max={360} min={100} onChange={(event) => updatePreferences({ outputHeight: Number(event.target.value) })} type="range" value={preferences.outputHeight} /><output>{preferences.outputHeight}px</output>
        </SettingRow>
        <SettingRow label="Кэш файлов" description="Лимит локального кэша файлов заданий. Очищать кэш можно здесь же.">
      <div className={styles.settingControlGroup}><select className={styles.settingSelect} aria-label="Размер кэша файлов" value={preferences.fileCacheMb} onChange={(event) => updatePreferences({ fileCacheMb: Number(event.target.value) as 32 | 64 | 128 | 256 | 512 | 1024 })}><option value={32}>32 МБ</option><option value={64}>64 МБ</option><option value={128}>128 МБ</option><option value={256}>256 МБ</option><option value={512}>512 МБ</option><option value={1024}>1024 МБ</option></select><button className={styles.resetButton} disabled={cacheBusy} type="button" onClick={() => void clearCache()}>{cacheBusy ? "Очищаем…" : "Очистить"}</button>{cacheNotice ? <span className={styles.inlineNotice} role="status">{cacheNotice}</span> : null}{cacheError ? <span className={styles.inlineError} role="alert">{cacheError}</span> : null}</div>
        </SettingRow>
      </div>
      <div className={styles.sectionActions}><button className={styles.resetButton} type="button" onClick={() => resetPreferenceSection("workspace")}>Сбросить раздел</button></div>
    </section>
  );
}

function BoardSection() {
  const { preferences, updatePreferences } = usePreferences();
  const [tool, setTool] = useState<"pen" | "marker">("pen");
  return (
    <section className={styles.panel} aria-labelledby="board-settings-title">
      <div className={styles.panelHeading}><div><span className="eyebrow">Поверхность решения</span><h2 id="board-settings-title">Доска</h2><p>Цвета и параметры штриха относятся к доске и не становятся темой интерфейса.</p></div></div>
      <div className={styles.settingList}>
        <SettingRow label="Инструмент" description="Настройки пера и маркера хранятся отдельно.">
          <SegmentedControl ariaLabel="Инструмент доски" value={tool} options={[{ value: "pen", label: "Перо" }, { value: "marker", label: "Маркер" }]} onChange={(value) => setTool(value as "pen" | "marker")} />
        </SettingRow>
        <SettingRow label={tool === "marker" ? "Цвет маркера" : "Цвет пера"} description="Цвет сохраняется отдельно от темы и режима просмотра.">
          <input aria-label={tool === "marker" ? "Цвет маркера" : "Цвет пера"} className={styles.colorInput} onChange={(event) => updatePreferences(tool==="marker"?{boardMarkerColor:event.target.value}:{boardColor:event.target.value})} type="color" value={tool==="marker"?preferences.boardMarkerColor:preferences.boardColor} />
        </SettingRow>
        <SettingRow label="Толщина" description="Толщина выбранного инструмента.">
          <input aria-label="Толщина штриха" className={styles.rangeInput} max={24} min={1} onChange={(event) => updatePreferences(tool==="marker"?{boardMarkerWidth:Number(event.target.value)}:{boardWidth:Number(event.target.value)})} type="range" value={tool==="marker"?preferences.boardMarkerWidth:preferences.boardWidth} /><output>{tool==="marker"?preferences.boardMarkerWidth:preferences.boardWidth}</output>
        </SettingRow>
        <SettingRow label="Непрозрачность" description="Параметр применяется к следующему штриху.">
          <input aria-label="Непрозрачность штриха" className={styles.rangeInput} max={1} min={0.1} onChange={(event) => updatePreferences(tool==="marker"?{boardMarkerOpacity:Number(event.target.value)}:{boardOpacity:Number(event.target.value)})} step={0.05} type="range" value={tool==="marker"?preferences.boardMarkerOpacity:preferences.boardOpacity} /><output>{Math.round((tool==="marker"?preferences.boardMarkerOpacity:preferences.boardOpacity)*100)}%</output>
        </SettingRow>
        <SettingRow label="Фон доски" description="Пустой, клетчатый или точечный фон.">
          <SegmentedControl ariaLabel="Фон доски" value={preferences.boardBackground} options={[{ value: "blank", label: "Пустой" }, { value: "grid", label: "Сетка" }, { value: "dots", label: "Точки" }]} onChange={(value) => updatePreferences({ boardBackground: value as "blank" | "grid" | "dots" })} />
        </SettingRow>
        <SettingRow label="Сглаживание пера" description="Сила сглаживания новых штрихов на доске.">
          <input aria-label="Сглаживание пера" className={styles.rangeInput} max={1} min={0} onChange={(event) => updatePreferences({ drawingSmoothing: Number(event.target.value) })} step={0.05} type="range" value={preferences.drawingSmoothing} /><output>{Math.round(preferences.drawingSmoothing * 100)}%</output>
        </SettingRow>
      </div>
      <div className={styles.sectionActions}><button className={styles.resetButton} type="button" onClick={() => resetPreferenceSection("board")}>Сбросить раздел</button></div>
    </section>
  );
}

function SettingRow({ label, description, children }: { label: string; description: string; children: React.ReactNode }) {
  return <div className={styles.settingRow}><div className={styles.settingCopy}><strong>{label}</strong><span>{description}</span></div><div className={styles.settingControl}>{children}</div></div>;
}

function SegmentedControl({ ariaLabel, value, options, onChange }: { ariaLabel: string; value: string; options: Array<{ value: string; label: string }>; onChange: (value: string) => void }) {
  return <div aria-label={ariaLabel} className={styles.segmentedControl} role="group">{options.map((option) => <button aria-pressed={option.value === value} className={option.value === value ? styles.segmentActive : ""} key={option.value} onClick={() => onChange(option.value)} type="button">{option.label}</button>)}</div>;
}

type SampleForm = { title: string; content: string; tags: string; taskLines: string; usageNote: string };

function SamplesSection({
  form,
  loading,
  saving,
  samples,
  setForm,
  onSubmit,
  onToggle,
}: {
  form: SampleForm;
  loading: boolean;
  saving: boolean;
  samples: CodeSample[];
  setForm: React.Dispatch<React.SetStateAction<SampleForm>>;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onToggle: (sample: CodeSample) => void;
}) {
  return (
    <section className={styles.panel} aria-labelledby="samples-title">
      <div className={styles.panelHeading}>
        <div>
          <span className="eyebrow">Ваш контекст</span>
          <h2 id="samples-title">Образцы кода</h2>
          <p>Сохраните свои шаблоны Python. Помощник учитывает их только после явного выбора в поле сообщения.</p>
        </div>
      </div>
      <div className={styles.samplesLayout}>
        <div className={styles.sampleSection}>
          <div className={styles.sectionLabel}>Сохранённые образцы</div>
          <h3 className={styles.sectionTitle}>Мои коды <span className="muted-inline">{loading ? "" : `· ${samples.length}`}</span></h3>
          <p className={styles.sectionDescription}>Отключённые образцы остаются сохранёнными, но не предлагаются наставнику.</p>
          <div className={styles.sampleList}>
            {samples.map((sample) => <SampleRow key={sample.id} sample={sample} onToggle={onToggle} />)}
          </div>
          {!loading && !samples.length ? <p className={styles.emptySamples}>Сохранённых образцов пока нет. Добавьте первый справа.</p> : null}
          {loading ? <p className={styles.emptySamples}>Загружаем ваши образцы…</p> : null}
        </div>

        <div className={styles.sampleFormSection}>
          <div className={styles.sectionLabel}>Новый образец</div>
          <h3 className={styles.sectionTitle}>Добавить код</h3>
          <p className={styles.sectionDescription}>Добавьте короткую заметку, чтобы понимать, когда шаблон пригодится.</p>
          <form className={styles.form} onSubmit={onSubmit}>
            <label><span>Название</span><input onChange={(event) => setForm((current) => ({ ...current, title: event.target.value }))} placeholder="Например, обход графа" value={form.title} /></label>
            <label><span>Код Python</span><textarea className="sample-code-input" onChange={(event) => setForm((current) => ({ ...current, content: event.target.value }))} placeholder="Вставьте свой шаблон…" rows={8} value={form.content} /></label>
            <div className={styles.formRow}><label><span>Номера заданий</span><input onChange={(event) => setForm((current) => ({ ...current, taskLines: event.target.value }))} placeholder="17, 24" value={form.taskLines} /></label><label><span>Теги</span><input onChange={(event) => setForm((current) => ({ ...current, tags: event.target.value }))} placeholder="графы, bfs" value={form.tags} /></label></div>
            <label><span>Когда использовать</span><input onChange={(event) => setForm((current) => ({ ...current, usageNote: event.target.value }))} placeholder="Короткая заметка для себя" value={form.usageNote} /></label>
            <button className="primary-button full-width" disabled={saving || !form.title.trim() || !form.content.trim()} type="submit">{saving ? <><span className="button-spinner" />Сохраняем…</> : <><Icon name="save" size={16} />Сохранить образец</>}</button>
          </form>
        </div>
      </div>
    </section>
  );
}

function SampleRow({ sample, onToggle }: { sample: CodeSample; onToggle: (sample: CodeSample) => void }) {
  return (
    <div className={`${styles.sampleRow} ${!sample.enabled ? styles.sampleRowDisabled : ""}`}>
      <span className={styles.sampleLanguage}>PY</span>
      <div className={styles.sampleCopy}>
        <strong>{sample.title}</strong>
        <small>{sample.usage_note || "Без заметки"}</small>
        <div className={styles.tagList}>{sample.tags.map((tag) => <span key={tag}>{tag}</span>)}{sample.task_lines.length ? <span>№ {sample.task_lines.join(", ")}</span> : null}</div>
      </div>
      <label className={styles.switchControl}><span className="sr-only">{sample.enabled ? "Отключить" : "Включить"} образец «{sample.title}»</span><input checked={sample.enabled} onChange={() => onToggle(sample)} type="checkbox" /><span className={styles.switchTrack} /></label>
    </div>
  );
}

function parseLines(value: string): number[] {
  return value.split(",").map((item) => Number.parseInt(item.trim(), 10)).filter((item) => Number.isInteger(item) && item > 0);
}

function getSection(querySection: string | null): SettingsSection {
  const hash = typeof window === "undefined" ? "" : decodeURIComponent(window.location.hash.replace(/^#/, "")).replace(/^section=/, "");
  const candidate = querySection ?? hash;
  return isSettingsSection(candidate) ? candidate : "interface";
}

function isSettingsSection(value: string | null): value is SettingsSection {
  return value === "interface" || value === "workspace" || value === "board" || value === "profile" || value === "assistant" || value === "shortcuts" || value === "samples" || value === "advanced" || value === "archive" || value === "data";
}

function getReturnPath(value: string | null): string {
  return value && value.startsWith("/") && !value.startsWith("//") && !value.includes("\\") && !/[\u0000-\u001F]/.test(value) ? value : "/tasks";
}
