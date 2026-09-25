import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Chat",
  // A visitor-facing frame, not a page anyone should land on from a search.
  robots: { index: false, follow: false },
};

/**
 * The chat widget's page, framed by a customer's site (spec §6).
 *
 * Deliberately bare: no `AuthProvider` and no urql -- the root layout carries
 * neither, and adding them here would start a dashboard session refresh
 * inside someone else's website. `data-widget-embed` is what turns the root
 * body transparent (see the rule at the end of `globals.css`); `globals.css`
 * itself comes from the root layout, which this nests inside.
 */
export default function EmbedLayout({ children }: { children: React.ReactNode }) {
  return (
    <div data-widget-embed className="h-dvh bg-transparent">
      {children}
    </div>
  );
}
