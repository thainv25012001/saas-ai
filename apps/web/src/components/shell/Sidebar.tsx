"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { cn } from "@/components/ui/cn";
import { Icon } from "@/components/ui/icons";
import { isActive, NAV_GROUPS } from "./nav";

export function Sidebar({
  organizationName,
  email,
  onSignOut,
  signingOut = false,
  onNavigate,
}: {
  organizationName: string;
  email: string;
  onSignOut: () => void;
  signingOut?: boolean;
  /** Called on any nav click, so the mobile drawer can close itself. */
  onNavigate?: () => void;
}) {
  const pathname = usePathname();

  return (
    <div className="flex h-full flex-col border-r border-line bg-surface">
      <div className="flex h-14 shrink-0 items-center gap-2 border-b border-line px-4">
        <span className="flex size-7 items-center justify-center rounded-control bg-primary text-primary-ink">
          <Icon name="agent" className="size-4" />
        </span>
        <span className="truncate text-sm font-semibold text-ink">AI Sales Agent</span>
      </div>

      <nav aria-label="Main" className="min-h-0 flex-1 overflow-y-auto px-3 py-4">
        {NAV_GROUPS.map((group) => (
          <div key={group.label ?? "root"} className="mb-4 last:mb-0">
            {group.label ? (
              <p className="px-3 pb-1.5 text-xs font-medium uppercase tracking-wide text-ink-subtle">
                {group.label}
              </p>
            ) : null}
            <ul className="space-y-0.5">
              {group.items.map((item) => {
                const active = isActive(pathname, item.href);
                return (
                  <li key={item.href}>
                    <Link
                      href={item.href}
                      onClick={onNavigate}
                      aria-current={active ? "page" : undefined}
                      className={cn(
                        "flex items-center gap-2.5 rounded-control px-3 py-2 text-sm transition-colors",
                        active
                          ? "bg-primary font-medium text-primary-ink"
                          : "text-ink-muted hover:bg-surface-muted hover:text-ink",
                      )}
                    >
                      <Icon name={item.icon} className="size-4" />
                      <span className="flex-1 truncate">{item.label}</span>
                      {/* Badged rather than hidden: the shape of the product is
                       * worth showing early, but a click should be informed. */}
                      {item.state === "soon" ? (
                        <Badge tone={active ? "neutral" : "warn"} className="shrink-0">
                          Soon
                        </Badge>
                      ) : null}
                    </Link>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </nav>

      <div className="shrink-0 border-t border-line p-3">
        <p className="truncate px-1 text-sm font-medium text-ink">{organizationName}</p>
        <p className="truncate px-1 text-xs text-ink-subtle">{email}</p>
        <Button
          variant="secondary"
          size="sm"
          onClick={onSignOut}
          loading={signingOut}
          loadingLabel="Signing out…"
          className="mt-3 w-full"
        >
          Sign out
        </Button>
      </div>
    </div>
  );
}
