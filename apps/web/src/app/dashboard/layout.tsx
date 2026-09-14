"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import { useAuth } from "@/lib/auth";

const NAV = [
  { href: "/dashboard", label: "Overview" },
  { href: "/dashboard/agents", label: "Agents" },
  { href: "/dashboard/knowledge", label: "Knowledge" },
  { href: "/dashboard/products", label: "Products" },
  { href: "/dashboard/leads", label: "Leads" },
  { href: "/dashboard/prompts", label: "Prompts" },
  { href: "/dashboard/playground", label: "Playground" },
];

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const { user, loading, logout } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (!loading && !user) router.replace("/login");
  }, [loading, user, router]);

  if (loading) return <p className="p-8 text-slate-500">Loading…</p>;
  if (!user) return null;

  return (
    <div className="flex min-h-screen">
      <aside className="w-60 shrink-0 border-r border-slate-200 bg-white p-4">
        <p className="px-3 text-sm font-semibold">{user.organization_name}</p>
        <p className="mb-4 px-3 text-xs text-slate-500">{user.email}</p>
        <nav className="space-y-1">
          {NAV.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className={`block rounded-md px-3 py-2 text-sm ${
                pathname === item.href
                  ? "bg-slate-900 text-white"
                  : "text-slate-700 hover:bg-slate-100"
              }`}
            >
              {item.label}
            </Link>
          ))}
        </nav>
        <button
          onClick={async () => {
            await logout();
            router.replace("/login");
          }}
          className="mt-6 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
        >
          Sign out
        </button>
      </aside>
      <main className="flex-1 p-8">{children}</main>
    </div>
  );
}
