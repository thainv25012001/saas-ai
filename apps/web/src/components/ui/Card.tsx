import { cn } from "./cn";

const CARD_TONES = {
  default: "border-line",
  danger: "border-danger-line",
} as const;

export function Card({
  tone = "default",
  className,
  children,
}: {
  tone?: "default" | "danger";
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div className={cn("rounded-card border bg-surface", CARD_TONES[tone], className)}>
      {children}
    </div>
  );
}

export function CardHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: React.ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-line px-5 py-4">
      <div className="min-w-0">
        <h2 className="text-sm font-semibold text-ink">{title}</h2>
        {description ? <p className="mt-1 text-xs text-ink-muted">{description}</p> : null}
      </div>
      {actions ? <div className="shrink-0">{actions}</div> : null}
    </div>
  );
}

const CARD_BODY_PADDING = {
  default: "px-5 py-4",
  loose: "p-6",
} as const;

export function CardBody({
  padding = "default",
  className,
  children,
}: {
  padding?: "default" | "loose";
  className?: string;
  children: React.ReactNode;
}) {
  return <div className={cn(CARD_BODY_PADDING[padding], className)}>{children}</div>;
}

export function CardFooter({ className, children }: { className?: string; children: React.ReactNode }) {
  return (
    <div className={cn("flex items-center gap-3 border-t border-line px-5 py-3", className)}>
      {children}
    </div>
  );
}
