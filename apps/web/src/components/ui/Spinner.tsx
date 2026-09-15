import { cn } from "./cn";

export function Spinner({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden="true"
      focusable="false"
      className={cn("size-4 shrink-0 animate-spin", className)}
    >
      <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeWidth="2.5" opacity="0.25" />
      <path
        d="M21 12a9 9 0 0 0-9-9"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

/** The one way this app says it is waiting. */
export function LoadingState({ label }: { label: string }) {
  return (
    <div role="status" className="flex items-center gap-2 px-5 py-8 text-sm text-ink-subtle">
      <Spinner />
      <span>{label}</span>
    </div>
  );
}
