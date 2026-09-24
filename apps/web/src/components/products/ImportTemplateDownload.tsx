"use client";

import { useState } from "react";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import type { ApiError } from "@/lib/api";
import type { DocumentAuth } from "@/lib/documents";
import { downloadImportTemplate, type ImportTemplateFormat } from "@/lib/products";

const FORMATS: { format: ImportTemplateFormat; label: string }[] = [
  { format: "csv", label: "CSV" },
  { format: "xlsx", label: "Excel" },
  { format: "json", label: "JSON" },
];

function saveFile(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

/**
 * A sample catalogue in each format the import accepts, with every column it
 * reads and a few example rows -- so the columns are learned from a file
 * that imports cleanly, not from the first import's failed-rows list.
 */
export function ImportTemplateDownload({ auth }: { auth: DocumentAuth }) {
  const [downloading, setDownloading] = useState<ImportTemplateFormat | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  async function handleDownload(format: ImportTemplateFormat) {
    setError(null);
    setDownloading(format);
    try {
      const { blob, filename } = await downloadImportTemplate(format, auth);
      saveFile(blob, filename);
    } catch (err) {
      setError(err as ApiError);
    } finally {
      setDownloading(null);
    }
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-ink-muted">Download a sample file:</span>
        {FORMATS.map(({ format, label }) => (
          <Button
            key={format}
            type="button"
            variant="secondary"
            size="sm"
            aria-label={`Download sample ${label}`}
            loading={downloading === format}
            disabled={downloading !== null}
            onClick={() => handleDownload(format)}
          >
            {label}
          </Button>
        ))}
      </div>
      {error ? <Alert tone="danger">{error.message}</Alert> : null}
    </div>
  );
}
