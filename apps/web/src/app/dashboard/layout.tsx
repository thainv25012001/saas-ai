"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { Sidebar } from "@/components/shell/Sidebar";
import { TopBar } from "@/components/shell/TopBar";
import { currentSectionLabel } from "@/components/shell/nav";
import { LoadingState } from "@/components/ui/Spinner";
import { cn } from "@/components/ui/cn";
import { AuthProvider, useAuth } from "@/lib/auth";
import { UrqlProvider } from "@/lib/urql";

/** Routes that fill the frame and manage their own internal scrolling, rather
 * than sitting in the centred content well. The playground's transcript is
 * the scroll container, which is what lets it drop the viewport arithmetic. */
const FULL_BLEED_ROUTES = new Set(["/dashboard/playground"]);

/** The providers live here and in `(auth)/layout.tsx`, not in the root
 * layout, so the public embed page never mounts them (see `app/layout.tsx`). */
export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  return (
    <AuthProvider>
      <UrqlProvider>
        <DashboardShell>{children}</DashboardShell>
      </UrqlProvider>
    </AuthProvider>
  );
}

function DashboardShell({ children }: { children: React.ReactNode }) {
  const { user, loading, logout } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const [navOpen, setNavOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);

  useEffect(() => {
    if (!loading && !user) router.replace("/login");
  }, [loading, user, router]);

  // Navigating is the drawer's natural dismissal: the user got where they
  // were going, and leaving it open would cover the page they asked for.
  useEffect(() => {
    setNavOpen(false);
  }, [pathname]);

  useEffect(() => {
    if (!navOpen) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setNavOpen(false);
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [navOpen]);

  async function onSignOut() {
    setSigningOut(true);
    try {
      await logout();
      router.replace("/login");
    } finally {
      setSigningOut(false);
    }
  }

  if (loading) return <LoadingState label="Loading your workspace…" />;
  if (!user) return null;

  const fullBleed = FULL_BLEED_ROUTES.has(pathname);

  return (
    // h-dvh + a single overflow-y-auto main is what removes the need for any
    // page to compute a height from the viewport.
    <div className="flex h-dvh overflow-hidden">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded-control focus:bg-primary focus:px-3 focus:py-2 focus:text-sm focus:text-primary-ink"
      >
        Skip to content
      </a>

      <div className="hidden w-64 shrink-0 lg:block">
        <Sidebar
          organizationName={user.organization_name}
          email={user.email}
          onSignOut={onSignOut}
          signingOut={signingOut}
        />
      </div>

      {navOpen ? (
        <div className="fixed inset-0 z-40 lg:hidden">
          <button
            type="button"
            aria-label="Close navigation"
            onClick={() => setNavOpen(false)}
            className="absolute inset-0 bg-ink/40"
          />
          <div className="absolute left-0 top-0 h-full w-64">
            <Sidebar
              organizationName={user.organization_name}
              email={user.email}
              onSignOut={onSignOut}
              signingOut={signingOut}
              onNavigate={() => setNavOpen(false)}
            />
          </div>
        </div>
      ) : null}

      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar sectionLabel={currentSectionLabel(pathname)} onOpenNav={() => setNavOpen(true)} />
        <main id="main" className={cn("min-h-0 flex-1 overflow-y-auto", !fullBleed && "px-6 py-8")}>
          {fullBleed ? children : <div className="mx-auto w-full max-w-6xl">{children}</div>}
        </main>
      </div>
    </div>
  );
}
