import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import type { CaseValues } from "./CaseForm";

export type CaseRow = CaseValues & { id: string };

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** The expectations a case carries, one chip each -- what it will be scored
 * on, readable at a glance without opening the case. Exported so the chips
 * and the form's own rule ("at least one") are tested against one list. */
export function expectationChips(row: CaseValues): string[] {
  const chips: string[] = [];
  if (row.referenceAnswer) chips.push("Reference answer");
  if (row.requiredPhrases.length > 0) chips.push(plural(row.requiredPhrases.length, "phrase", "phrases"));
  for (const tool of row.expectedToolNames) chips.push(tool);
  if (row.expectedDocumentIds.length > 0) {
    chips.push(plural(row.expectedDocumentIds.length, "document", "documents"));
  }
  if (row.expectedProductIds.length > 0) {
    chips.push(plural(row.expectedProductIds.length, "product", "products"));
  }
  return chips;
}

/**
 * A dataset's cases. Presentational; the page owns the mutations. The
 * question and the tags are the customer's own text -- JSX text only.
 */
export function CasesTable({
  cases,
  onEdit,
  onDelete,
  deletingId = null,
  editingId = null,
}: {
  cases: readonly CaseRow[];
  onEdit: (row: CaseRow) => void;
  onDelete: (id: string) => void;
  deletingId?: string | null;
  editingId?: string | null;
}) {
  if (cases.length === 0) {
    return (
      <EmptyState
        icon="evaluation"
        title="No cases yet"
        description="Add a question and what a good answer contains. A run needs at least one case."
      />
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-line bg-surface-muted text-xs uppercase tracking-wide text-ink-subtle">
          <tr>
            <th scope="col" className="px-5 py-2.5 font-medium">
              Question
            </th>
            <th scope="col" className="px-5 py-2.5 font-medium">
              Expects
            </th>
            <th scope="col" className="px-5 py-2.5 font-medium">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {cases.map((row) => (
            <tr key={row.id} className="align-top">
              <td className="max-w-md px-5 py-3">
                <p className="line-clamp-3 whitespace-pre-wrap text-ink" title={row.question}>
                  {row.question}
                </p>
                {row.tags.length > 0 ? (
                  <p className="mt-1 text-xs text-ink-subtle">{row.tags.join(" · ")}</p>
                ) : null}
              </td>
              <td className="px-5 py-3">
                <div className="flex flex-wrap gap-1.5">
                  {expectationChips(row).map((chip) => (
                    <Badge key={chip}>{chip}</Badge>
                  ))}
                </div>
              </td>
              <td className="whitespace-nowrap px-5 py-3 text-right">
                <div className="inline-flex gap-2">
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => onEdit(row)}
                    disabled={editingId === row.id}
                    aria-label={`Edit case: ${row.question.slice(0, 60)}`}
                  >
                    Edit
                  </Button>
                  <Button
                    size="sm"
                    variant="danger"
                    onClick={() => onDelete(row.id)}
                    loading={deletingId === row.id}
                    loadingLabel="Deleting…"
                    aria-label={`Delete case: ${row.question.slice(0, 60)}`}
                  >
                    Delete
                  </Button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
