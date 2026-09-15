import Link from "next/link";
import { forwardRef } from "react";
import { cn } from "./cn";

export type ButtonVariant = "primary" | "secondary" | "danger";
export type ButtonSize = "sm" | "md";

const BASE =
  "inline-flex items-center justify-center gap-2 rounded-control font-medium transition-colors " +
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink focus-visible:ring-offset-2 " +
  "focus-visible:ring-offset-canvas disabled:cursor-not-allowed disabled:opacity-50";

const VARIANTS: Record<ButtonVariant, string> = {
  primary: "bg-primary text-primary-ink hover:bg-primary-hover",
  secondary: "border border-line-strong bg-surface text-ink hover:bg-surface-muted",
  danger: "border border-danger-line bg-surface text-danger hover:bg-danger-surface",
};

const SIZES: Record<ButtonSize, string> = {
  sm: "px-3 py-1.5 text-sm",
  md: "px-4 py-2 text-sm",
};

/** Shared so `ButtonLink` and `Button` cannot drift apart. */
export function buttonClasses({
  variant = "primary",
  size = "md",
  className,
}: {
  variant?: ButtonVariant;
  size?: ButtonSize;
  className?: string;
} = {}): string {
  return cn(BASE, VARIANTS[variant], SIZES[size], className);
}

export type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Disables the button and, with `loadingLabel`, swaps the label. */
  loading?: boolean;
  loadingLabel?: string;
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant, size, loading = false, loadingLabel, className, disabled, children, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      // Every call site before this component wrote `disabled={fetching}` and
      // `{fetching ? "Saving…" : "Save"}` by hand; both now follow from one prop.
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={buttonClasses({ variant, size, className })}
      {...rest}
    >
      {loading && loadingLabel ? loadingLabel : children}
    </button>
  );
});

export type ButtonLinkProps = React.ComponentProps<typeof Link> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
};

export function ButtonLink({ variant, size, className, ...rest }: ButtonLinkProps) {
  return <Link className={buttonClasses({ variant, size, className })} {...rest} />;
}
