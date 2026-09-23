"use client";

import Link from "next/link";
import { useState } from "react";
import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Field } from "@/components/ui/Field";
import { Input, Textarea } from "@/components/ui/Input";
import type { EvaluationRunStatus } from "@/graphql/generated";
import { formatPassRate, runStatusLabel, runStatusTone } from "@/lib/evaluations";

export type DatasetRow = {
  id: string;
  name: string;
  description: string | null;
  caseCount: number;
  latestRun: { status: EvaluationRunStatus; passRate: number | null } | null;
};

/**
 * The datasets an organization has, with how the latest run of each went.
 * Presentational: the page owns the query. Names and descriptions are the
 * customer's own text, rendered as JSX text only.
 */
export function DatasetList({ datasets }: { datasets: readonly DatasetRow[] }) {
  if (datasets.length === 0) {
    return (
      <EmptyState
        icon="evaluation"
        title="No datasets yet"
        description="A dataset is a set of questions with the answers you expect, run against an agent to check a prompt or model change before customers see it."
      />
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-line bg-surface-muted text-xs uppercase tracking-wide text-ink-subtle">
          <tr>
            <th scope="col" className="px-5 py-2.5 font-medium">
              Dataset
            </th>
            <th scope="col" className="px-5 py-2.5 font-medium">
              Cases
            </th>
            <th scope="col" className="px-5 py-2.5 font-medium">
              Latest run
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {datasets.map((dataset) => (
            <tr key={dataset.id} className="align-top">
              <td className="max-w-md px-5 py-3">
                <Link
                  href={`/dashboard/evaluations/${dataset.id}`}
                  className="font-medium text-ink hover:underline"
                >
                  {dataset.name}
                </Link>
                {dataset.description ? (
                  <p className="mt-0.5 line-clamp-2 text-xs text-ink-muted">{dataset.description}</p>
                ) : null}
              </td>
              <td className="px-5 py-3 text-ink">{dataset.caseCount}</td>
              <td className="px-5 py-3">
                {dataset.latestRun ? (
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge tone={runStatusTone(dataset.latestRun.status)}>
                      {runStatusLabel(dataset.latestRun.status)}
                    </Badge>
                    {dataset.latestRun.status === "COMPLETED" ? (
                      <span className="text-ink">{formatPassRate(dataset.latestRun.passRate)} passed</span>
                    ) : null}
                  </div>
                ) : (
                  <span className="text-ink-subtle">Never run</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Name plus an optional description. Resets itself after a successful
 * create; the page decides where to go next. */
export function CreateDatasetForm({
  onCreate,
  submitting = false,
  error = null,
}: {
  onCreate: (values: { name: string; description: string | null }) => Promise<boolean> | boolean;
  submitting?: boolean;
  error?: string | null;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) return;
    const created = await onCreate({ name: trimmed, description: description.trim() || null });
    if (created) {
      setName("");
      setDescription("");
    }
  }

  return (
    <Card>
      <form onSubmit={onSubmit}>
        <CardHeader title="New dataset" description="Group the questions you want to check together." />
        <CardBody className="space-y-4">
          {error ? <Alert tone="danger">{error}</Alert> : null}
          <Field label="Name" required>
            {(control) => (
              <Input
                {...control}
                type="text"
                maxLength={200}
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="Pricing questions"
              />
            )}
          </Field>
          <Field label="Description">
            {(control) => (
              <Textarea
                {...control}
                rows={2}
                maxLength={2000}
                value={description}
                onChange={(event) => setDescription(event.target.value)}
              />
            )}
          </Field>
        </CardBody>
        <CardFooter>
          <Button type="submit" disabled={!name.trim()} loading={submitting} loadingLabel="Creating…">
            Create dataset
          </Button>
        </CardFooter>
      </form>
    </Card>
  );
}
