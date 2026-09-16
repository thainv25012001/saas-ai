import { forwardRef } from "react";
import { cn, focusRing } from "./cn";
import { Icon } from "./icons";

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
  const full = width === "full";
  return (
    // `appearance-none` plus our own chevron, rather than the arrow the OS
    // paints: that one is drawn flush at the right edge in a colour no token
    // controls and a size `text-sm` does not reach, so a Select sat beside an
    // Input read as a different control. `pr-9` is what clears the chevron
    // below -- `right-3` plus its `size-4`.
    <span className={cn("relative inline-flex items-center", full ? "w-full" : "w-auto")}>
      <select
        ref={ref}
        className={cn(
          controlClasses,
          "appearance-none truncate pr-9",
          full ? "w-full" : "w-auto",
          className,
        )}
        {...rest}
      />
      <Icon
        name="chevronDown"
        size="md"
        // `pointer-events-none` so the chevron cannot swallow the click that
        // is meant to open the select underneath it.
        className="pointer-events-none absolute right-3 text-ink-subtle"
      />
    </span>
  );
});
