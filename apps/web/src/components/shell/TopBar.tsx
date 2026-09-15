"use client";

import { focusRing } from "@/components/ui/cn";
import { Icon } from "@/components/ui/icons";

/** Mobile only. On `lg` and up the sidebar is always visible, so a second
 * header would just cost vertical space the playground wants. */
export function TopBar({
  sectionLabel,
  onOpenNav,
}: {
  sectionLabel: string;
  onOpenNav: () => void;
}) {
  return (
    <header className="flex h-14 shrink-0 items-center gap-3 border-b border-line bg-surface px-4 lg:hidden">
      <button
        type="button"
        onClick={onOpenNav}
        aria-label="Open navigation"
        className={`-ml-1 rounded-control p-1.5 text-ink-muted hover:bg-surface-muted hover:text-ink ${focusRing} focus-visible:ring-offset-2`}
      >
        <Icon name="menu" />
      </button>
      <span className="truncate text-sm font-semibold text-ink">{sectionLabel}</span>
    </header>
  );
}
