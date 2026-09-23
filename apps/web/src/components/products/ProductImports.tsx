import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import type { ProductImportStatus } from "@/graphql/generated";
import { formatTimestamp } from "@/lib/format";
import { importStatusLabel, importStatusTone } from "@/lib/product-status";

export type ProductImportRow = {
  id: string;
  filename: string | null;
  status: ProductImportStatus;
  totalRows: number | null;
  succeededCount: number;
  failedCount: number;
  /** On a failed import, the whole-file failure -- set instead of any
   * per-row error. On a completed one, a warning about the import as a whole
   * (today: the rows landed but could not all be embedded). */
  error: string | null;
  createdAt: string;
  /** The first page of failed rows, by row number; `failedCount` is the
   * total. */
  errors: readonly { row: number; externalId: string | null; message: string }[];
};

/**
 * Recent imports with their outcome. The per-row failures are the point: "4
 * rows failed" tells a customer something is wrong, and only the row numbers
 * (Task 3's 1-based, header-aware ones -- the line they would point at in
 * their own spreadsheet) and the reasons tell them what to fix.
 *
 * The file name, SKUs and messages can all quote the uploaded file, so they
 * are JSX text children only, like every product field.
 */
export function ProductImports({ imports }: { imports: readonly ProductImportRow[] }) {
  if (imports.length === 0) return null;

  return (
    <ul className="divide-y divide-line">
      {imports.map((record) => (
        <li key={record.id} className="space-y-2 px-5 py-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <span className="truncate text-sm font-medium text-ink" title={record.filename ?? undefined}>
                {record.filename ?? "Untitled file"}
              </span>
              <Badge tone={importStatusTone(record.status, record.failedCount)}>
                {importStatusLabel(record.status, record.failedCount)}
              </Badge>
            </div>
            <span className="text-xs text-ink-subtle">{formatTimestamp(record.createdAt)}</span>
          </div>

          {record.totalRows !== null ? (
            <p className="text-xs text-ink-muted">
              {record.succeededCount} of {record.totalRows} rows imported
              {record.failedCount > 0 ? `, ${record.failedCount} failed` : ""}.
            </p>
          ) : null}

          {record.status === "FAILED" && record.error ? (
            <Alert tone="danger">{record.error}</Alert>
          ) : null}

          {/* The rows are in; only search by meaning is missing for some of
            * them, which the product list badges row by row. A warning, not
            * a failure -- and it says how to retry. */}
          {record.status === "COMPLETED" && record.error ? (
            <Alert tone="warn">{record.error}</Alert>
          ) : null}

          {record.errors.length > 0 ? <RowErrors record={record} /> : null}
        </li>
      ))}
    </ul>
  );
}

function RowErrors({ record }: { record: ProductImportRow }) {
  const shown = record.errors.length;
  return (
    <details className="rounded-control border border-line" open={record.failedCount <= 10}>
      <summary className="cursor-pointer px-3 py-2 text-xs font-medium text-ink">
        {shown < record.failedCount
          ? `Failed rows (first ${shown} of ${record.failedCount})`
          : `Failed rows (${record.failedCount})`}
      </summary>
      <div className="overflow-x-auto border-t border-line">
        <table className="w-full text-left text-xs">
          <thead className="bg-surface-muted text-ink-subtle">
            <tr>
              <th scope="col" className="px-3 py-1.5 font-medium">
                Row
              </th>
              <th scope="col" className="px-3 py-1.5 font-medium">
                SKU
              </th>
              <th scope="col" className="px-3 py-1.5 font-medium">
                Problem
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {record.errors.map((error, index) => (
              // Index in the key: a row number can repeat (the same row can
              // fail more than one check), and the list never reorders.
              <tr key={`${error.row}-${index}`}>
                <td className="px-3 py-1.5 font-mono text-ink">{error.row}</td>
                <td className="px-3 py-1.5 font-mono text-ink-muted">
                  {error.externalId ?? <span className="text-ink-subtle">—</span>}
                </td>
                <td className="px-3 py-1.5 text-ink-muted">{error.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}
