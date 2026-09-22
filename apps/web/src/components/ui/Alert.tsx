import { cn } from "./cn";
import { Icon } from "./icons";

export type AlertTone = "danger" | "success" | "info" | "warn";

const TONES: Record<AlertTone, { classes: string; role: "alert" | "status" }> = {
  // `alert` interrupts a screen reader; `status` waits its turn. A save
  // confirmation is not worth an interruption, and a failure is.
  danger: { classes: "border-danger-line bg-danger-surface text-danger", role: "alert" },
  success: { classes: "border-success-line bg-success-surface text-success", role: "status" },
  info: { classes: "border-info-line bg-info-surface text-info", role: "status" },
  // `status`, not `alert`: a step-limit turn is a real, named outcome the
  // agent stopped at a configured boundary, not a crash -- see
  // `ChatMessage`'s handling of `step_limit_reached`. It is not "danger"
  // (nothing failed) and not "info" (it needs to read as incomplete, not
  // neutral), which is exactly `warn`'s meaning per `docs/DESIGN.md`.
  warn: { classes: "border-warn-line bg-warn-surface text-warn", role: "status" },
};

export function Alert({
  tone,
  title,
  children,
  className,
}: {
  tone: AlertTone;
  title?: string;
  children: React.ReactNode;
  className?: string;
}) {
  const { classes, role } = TONES[tone];
  return (
    <div
      role={role}
      className={cn("flex gap-2.5 rounded-control border p-3 text-sm", classes, className)}
    >
      {tone === "danger" || tone === "warn" ? (
        <Icon name="warning" size="md" className="mt-px" />
      ) : null}
      <div className="min-w-0">
        {title ? <p className="font-semibold">{title}</p> : null}
        <div className={cn(title && "mt-0.5")}>{children}</div>
      </div>
    </div>
  );
}
