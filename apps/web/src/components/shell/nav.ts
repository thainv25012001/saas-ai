import type { IconName } from "@/components/ui/icons";

export type NavItem = {
  href: string;
  label: string;
  icon: IconName;
  /** `soon` items link to a PlaceholderPage and carry a visible badge. */
  state: "live" | "soon";
  /** Which phase the section arrives in. Required for `soon` items. */
  phase?: string;
};

export type NavGroup = { label: string | null; items: NavItem[] };

/**
 * Grouped to mirror the product's loop -- configure the assistant, then run
 * it -- which is the cheapest way to make the sidebar explain the product.
 *
 * The not-yet-built sections stay in the nav deliberately (the shape of the
 * product is worth showing early), but they are badged, so a click on one is
 * an informed click rather than a dead end.
 */
export const NAV_GROUPS: NavGroup[] = [
  {
    label: null,
    items: [{ href: "/dashboard", label: "Overview", icon: "overview", state: "live" }],
  },
  {
    label: "Configure",
    items: [
      { href: "/dashboard/agents", label: "Agents", icon: "agent", state: "live" },
      { href: "/dashboard/prompts", label: "Prompts", icon: "prompt", state: "soon", phase: "Phase 2" },
      { href: "/dashboard/knowledge", label: "Knowledge", icon: "knowledge", state: "live" },
      { href: "/dashboard/products", label: "Products", icon: "product", state: "soon", phase: "Phase 4" },
    ],
  },
  {
    label: "Run",
    items: [
      { href: "/dashboard/playground", label: "Playground", icon: "playground", state: "live" },
      { href: "/dashboard/leads", label: "Leads", icon: "lead", state: "live" },
    ],
  },
];

/**
 * `/dashboard` matches only itself, because every route lives under it.
 * Everything else matches its own page and anything nested below it -- the
 * trailing slash is what keeps `/dashboard/agentsomething` from matching
 * `/dashboard/agents`.
 */
export function isActive(pathname: string, href: string): boolean {
  if (href === "/dashboard") return pathname === "/dashboard";
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function currentSectionLabel(pathname: string): string {
  for (const group of NAV_GROUPS) {
    for (const item of group.items) {
      if (isActive(pathname, item.href)) return item.label;
    }
  }
  return "Dashboard";
}
