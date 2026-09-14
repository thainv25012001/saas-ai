"use client";

import Link from "next/link";
import { useQuery } from "urql";
import { AgentsDocument } from "@/graphql/generated";
import { useAuth } from "@/lib/auth";

export default function DashboardPage() {
  const { user, loading } = useAuth();
  const [{ data, fetching }] = useQuery({
    query: AgentsDocument,
    pause: loading || !user,
  });
  const agents = data?.agents ?? [];

  return (
    <section className="space-y-6">
      <h1 className="text-2xl font-semibold">Overview</h1>
      <div className="rounded-xl border border-slate-200 bg-white p-6">
        <p className="text-sm text-slate-500">Agents</p>
        <p className="text-3xl font-semibold">{fetching ? "—" : agents.length}</p>
        <Link href="/dashboard/agents" className="mt-4 inline-block text-sm underline">
          Manage agents
        </Link>
      </div>
    </section>
  );
}
