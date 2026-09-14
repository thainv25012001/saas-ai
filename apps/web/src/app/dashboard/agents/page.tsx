"use client";

import Link from "next/link";
import { useState } from "react";
import { useMutation, useQuery } from "urql";
import { AgentsDocument, CreateAgentDocument } from "@/graphql/generated";
import { useAuth } from "@/lib/auth";

export default function AgentsPage() {
  const { user, loading } = useAuth();
  const [{ data, fetching }, refetchAgents] = useQuery({
    query: AgentsDocument,
    pause: loading || !user,
  });
  const [createResult, createAgent] = useMutation(CreateAgentDocument);
  const [name, setName] = useState("");

  const agents = data?.agents ?? [];

  async function onCreate(event: React.FormEvent) {
    event.preventDefault();
    const result = await createAgent({ name });
    if (!result.error) {
      setName("");
      refetchAgents({ requestPolicy: "network-only" });
    }
  }

  const createError = createResult.error?.graphQLErrors[0]?.message ?? null;

  return (
    <section className="space-y-6">
      <h1 className="text-2xl font-semibold">Agents</h1>

      <form
        onSubmit={onCreate}
        className="flex max-w-md items-end gap-3 rounded-xl border border-slate-200 bg-white p-6"
      >
        <label className="flex-1 text-sm font-medium">
          New agent name
          <input
            type="text"
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
          />
        </label>
        <button
          type="submit"
          disabled={createResult.fetching}
          className="rounded-md bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-50"
        >
          {createResult.fetching ? "Creating…" : "Create"}
        </button>
      </form>

      {createError && (
        <p role="alert" className="rounded-md bg-red-50 p-3 text-sm text-red-700">
          {createError}
        </p>
      )}

      <div className="rounded-xl border border-slate-200 bg-white">
        {fetching ? (
          <p className="p-6 text-sm text-slate-500">Loading…</p>
        ) : agents.length === 0 ? (
          <p className="p-6 text-sm text-slate-500">
            No agents yet. Create your first one above.
          </p>
        ) : (
          <table className="w-full text-left text-sm">
            <thead className="border-b border-slate-200 text-slate-500">
              <tr>
                <th className="px-6 py-3 font-medium">Name</th>
                <th className="px-6 py-3 font-medium">Slug</th>
                <th className="px-6 py-3 font-medium">Status</th>
                <th className="px-6 py-3 font-medium">Model</th>
              </tr>
            </thead>
            <tbody>
              {agents.map((agent) => (
                <tr key={String(agent.id)} className="border-b border-slate-100 last:border-0">
                  <td className="px-6 py-3">
                    <Link
                      href={`/dashboard/agents/${agent.id}`}
                      className="font-medium text-slate-900 underline"
                    >
                      {agent.name}
                    </Link>
                  </td>
                  <td className="px-6 py-3 text-slate-600">{agent.slug}</td>
                  <td className="px-6 py-3 text-slate-600">{agent.status}</td>
                  <td className="px-6 py-3 text-slate-600">{agent.model}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}
