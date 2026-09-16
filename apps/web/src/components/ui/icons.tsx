import { cn } from "./cn";

export type IconName =
  | "overview"
  | "agent"
  | "knowledge"
  | "product"
  | "lead"
  | "prompt"
  | "playground"
  | "chevronRight"
  | "chevronDown"
  | "plus"
  | "menu"
  | "close"
  | "check"
  | "circle"
  | "warning"
  | "send"
  | "stop";

const GLYPHS: Record<IconName, React.ReactNode> = {
  overview: (
    <>
      <rect x="3" y="3" width="8" height="8" rx="2" />
      <rect x="13" y="3" width="8" height="8" rx="2" />
      <rect x="3" y="13" width="8" height="8" rx="2" />
      <rect x="13" y="13" width="8" height="8" rx="2" />
    </>
  ),
  agent: (
    <>
      <path d="M4 5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H9l-5 4z" />
      <circle cx="9.5" cy="9.5" r="1" fill="currentColor" stroke="none" />
      <circle cx="14.5" cy="9.5" r="1" fill="currentColor" stroke="none" />
    </>
  ),
  knowledge: (
    <>
      <path d="M6 3h8l5 5v13H6z" />
      <path d="M14 3v5h5" />
      <path d="M9 13h6" />
      <path d="M9 17h6" />
    </>
  ),
  product: (
    <>
      <path d="M12 3l9 4.5v9L12 21l-9-4.5v-9z" />
      <path d="M3 7.5L12 12l9-4.5" />
      <path d="M12 12v9" />
    </>
  ),
  lead: (
    <>
      <circle cx="12" cy="8" r="3.5" />
      <path d="M5 20a7 7 0 0 1 14 0" />
    </>
  ),
  prompt: (
    <>
      <path d="M9 4H7.5A2.5 2.5 0 0 0 5 6.5v3A2.5 2.5 0 0 1 2.5 12A2.5 2.5 0 0 1 5 14.5v3A2.5 2.5 0 0 0 7.5 20H9" />
      <path d="M15 4h1.5A2.5 2.5 0 0 1 19 6.5v3A2.5 2.5 0 0 0 21.5 12A2.5 2.5 0 0 0 19 14.5v3A2.5 2.5 0 0 1 16.5 20H15" />
    </>
  ),
  playground: (
    <>
      <rect x="3" y="3" width="18" height="18" rx="4" />
      <path d="M10 8.5L16 12l-6 3.5z" />
    </>
  ),
  chevronRight: <path d="M9 6l6 6-6 6" />,
  chevronDown: <path d="M6 9l6 6 6-6" />,
  plus: (
    <>
      <path d="M12 5v14" />
      <path d="M5 12h14" />
    </>
  ),
  menu: (
    <>
      <path d="M4 7h16" />
      <path d="M4 12h16" />
      <path d="M4 17h16" />
    </>
  ),
  close: (
    <>
      <path d="M6 6l12 12" />
      <path d="M18 6L6 18" />
    </>
  ),
  check: <path d="M5 13l4 4 10-10" />,
  circle: <circle cx="12" cy="12" r="8" />,
  warning: (
    <>
      <path d="M12 4l9 15H3z" />
      <path d="M12 10v4" />
      <circle cx="12" cy="16.75" r="0.75" fill="currentColor" stroke="none" />
    </>
  ),
  send: (
    <>
      <path d="M21 4L3 11l7 2.5L12.5 21z" />
      <path d="M21 4l-11 9.5" />
    </>
  ),
  stop: <rect x="7" y="7" width="10" height="10" rx="2" />,
};

export type IconSize = "sm" | "md" | "lg";

const SIZES: Record<IconSize, string> = {
  sm: "size-3.5",
  md: "size-4",
  lg: "size-5",
};

/** Decorative by default: every icon in this UI sits beside its own label. */
export function Icon({
  name,
  size = "lg",
  className,
}: {
  name: IconName;
  size?: IconSize;
  className?: string;
}) {
  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden="true"
      focusable="false"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={cn("shrink-0", SIZES[size], className)}
    >
      {GLYPHS[name]}
    </svg>
  );
}
