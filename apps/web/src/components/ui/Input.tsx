import { forwardRef } from "react";
import { cn, focusRing } from "./cn";

/** One border, one radius, one focus ring — for all three controls. */
export const controlClasses =
  "rounded-control border border-line-strong bg-surface px-3 py-2 text-sm text-ink " +
  `placeholder:text-ink-subtle ${focusRing} focus-visible:ring-offset-1 ` +
  "disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-danger";

export const Input = forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, ...rest }, ref) {
    return <input ref={ref} className={cn(controlClasses, "w-full", className)} {...rest} />;
  },
);

const RESIZE = {
  none: "resize-none",
  y: "resize-y",
} as const;

export const Textarea = forwardRef<
  HTMLTextAreaElement,
  React.TextareaHTMLAttributes<HTMLTextAreaElement> & { resize?: "none" | "y" }
>(function Textarea({ resize = "none", className, ...rest }, ref) {
  return (
    <textarea
      ref={ref}
      className={cn(controlClasses, "w-full", RESIZE[resize], className)}
      {...rest}
    />
  );
});

export const Select = forwardRef<
  HTMLSelectElement,
  React.SelectHTMLAttributes<HTMLSelectElement> & { width?: "full" | "auto" }
>(function Select({ width = "full", className, ...rest }, ref) {
  return (
    <select
      ref={ref}
      className={cn(controlClasses, "pr-8", width === "full" ? "w-full" : "w-auto", className)}
      {...rest}
    />
  );
});
