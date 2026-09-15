import { cn } from "./cn";

export type BadgeTone = "neutral" | "success" | "warn" | "info";

const TONES: Record<BadgeTone, string> = {
  neutral: "border-line bg-surface-muted text-ink-muted",
  success: "border-success-line bg-success-surface text-success",
  warn: "border-warn-line bg-warn-surface text-warn",
  info: "border-info-line bg-info-surface text-info",
};

export function Badge({
  tone = "neutral",
  children,
  className,
  title,
}: {
  tone?: BadgeTone;
  children: React.ReactNode;
  className?: string;
  /** Native tooltip. The playground's meta chips use it to name the value. */
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center gap-1 rounded-control border px-2 py-0.5 text-xs font-medium",
        TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}
