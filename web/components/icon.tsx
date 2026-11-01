import type { CSSProperties, SVGProps } from "react";

export type IconName =
  | "plus"
  | "calendar"
  | "bookmark"
  | "book"
  | "target"
  | "warning"
  | "exam"
  | "chart"
  | "folder"
  | "chat"
  | "search"
  | "settings"
  | "filter"
  | "chevron-down"
  | "chevron-right"
  | "menu"
  | "panel"
  | "paperclip"
  | "send"
  | "play"
  | "stop"
  | "download"
  | "file"
  | "code"
  | "board"
  | "attempts"
  | "eraser"
  | "pen"
  | "pencil"
  | "highlighter"
  | "line"
  | "text"
  | "undo"
  | "redo"
  | "zoom-in"
  | "zoom-out"
  | "check"
  | "lock"
  | "external"
  | "dots"
  | "close"
  | "spark"
  | "info"
  | "refresh"
  | "save"
  | "user"
  | "logout"
  | "chevron-left"
  | "arrow-up"
  | "circle"
  | "mouse-pointer"
  | "maximize"
  | "help"
  | "shield"
  | "globe";

/**
 * Stable application names mapped to local Feather assets.
 *
 * The aliases keep the domain vocabulary used by the application while the
 * actual artwork stays in public/icons and is served same-origin.
 */
const assets: Record<IconName, string> = {
  plus: "plus.svg",
  calendar: "calendar.svg",
  bookmark: "bookmark.svg",
  book: "book-open.svg",
  target: "target.svg",
  warning: "alert-triangle.svg",
  exam: "file-text.svg",
  chart: "bar-chart-2.svg",
  folder: "folder.svg",
  chat: "message-circle.svg",
  search: "search.svg",
  settings: "settings.svg",
  filter: "filter.svg",
  "chevron-down": "chevron-down.svg",
  "chevron-right": "chevron-right.svg",
  menu: "menu.svg",
  panel: "sidebar.svg",
  paperclip: "paperclip.svg",
  send: "send.svg",
  play: "play.svg",
  stop: "square.svg",
  download: "download.svg",
  file: "file-text.svg",
  code: "code.svg",
  board: "grid.svg",
  attempts: "clipboard.svg",
  eraser: "delete.svg",
  pen: "edit-3.svg",
  pencil: "edit-3.svg",
  highlighter: "edit-3.svg",
  line: "minus.svg",
  text: "type.svg",
  undo: "corner-up-left.svg",
  redo: "corner-up-right.svg",
  "zoom-in": "zoom-in.svg",
  "zoom-out": "zoom-out.svg",
  check: "check.svg",
  lock: "lock.svg",
  external: "external-link.svg",
  dots: "more-horizontal.svg",
  close: "x.svg",
  spark: "star.svg",
  info: "info.svg",
  refresh: "refresh-cw.svg",
  save: "save.svg",
  user: "user.svg",
  logout: "log-out.svg",
  "chevron-left": "chevron-left.svg",
  "arrow-up": "arrow-up.svg",
  circle: "circle.svg",
  "mouse-pointer": "mouse-pointer.svg",
  maximize: "maximize.svg",
  help: "help-circle.svg",
  shield: "shield.svg",
  globe: "globe.svg",
};

type IconProps = Omit<SVGProps<SVGSVGElement>, "children"> & {
  name: IconName;
  size?: number;
  /** Kept for source compatibility; Feather assets use their canonical stroke. */
  strokeWidth?: number;
  title?: string;
};

/**
 * Render a local, official Feather SVG as a currentColor mask.
 *
 * Using a mask keeps the asset same-origin and lets the icon inherit the
 * surrounding text color without embedding or recreating SVG path data here.
 */
export function Icon({
  name,
  size = 18,
  strokeWidth = 1.75,
  className,
  style,
  title,
  "aria-hidden": ariaHidden,
  "aria-label": ariaLabel,
  "aria-labelledby": ariaLabelledBy,
  role,
  ...props
}: IconProps) {
  const hasAccessibleLabel = Boolean(title || ariaLabel || ariaLabelledBy);
  const explicitlyHidden = ariaHidden === true || ariaHidden === "true";
  const iconStyle: CSSProperties = {
    display: "inline-block",
    width: size,
    height: size,
    flex: "0 0 auto",
    backgroundColor: "currentColor",
    maskImage: `url("/icons/${assets[name]}")`,
    maskPosition: "center",
    maskRepeat: "no-repeat",
    maskSize: "contain",
    WebkitMaskImage: `url("/icons/${assets[name]}")`,
    WebkitMaskPosition: "center",
    WebkitMaskRepeat: "no-repeat",
    WebkitMaskSize: "contain",
    ...style,
  };

  return (
    <svg
      {...props}
      aria-hidden={ariaHidden ?? (hasAccessibleLabel ? undefined : true)}
      aria-label={ariaLabel ?? (title && !ariaLabelledBy ? title : undefined)}
      className={className}
      data-icon={name}
      data-stroke-width={strokeWidth}
      fill="none"
      height={size}
      role={role ?? (hasAccessibleLabel && !explicitlyHidden ? "img" : undefined)}
      style={iconStyle}
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth={strokeWidth}
      viewBox="0 0 24 24"
      width={size}
    >
      {title ? <title>{title}</title> : null}
    </svg>
  );
}
