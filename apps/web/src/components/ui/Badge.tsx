import { cn } from "./cn";

export type BadgeTone = "neutral" | "success" | "warn" | "info" | "danger";

const TONES: Record<BadgeTone, string> = {
  neutral: "border-line bg-surface-muted text-ink-muted",
  success: "border-success-line bg-success-surface text-success",
  warn: "border-warn-line bg-warn-surface text-warn",
  info: "border-info-line bg-info-surface text-info",
  // Added for document status (Phase 3): `failed` needs to read as wrong,
  // not merely as unfinished, and `warn` (already used for DRAFT/"soon") is
  // "not live yet", not "broke". The classes mirror Alert's `danger` tone,
  // which already draws on this same token trio.
  danger: "border-danger-line bg-danger-surface text-danger",
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
