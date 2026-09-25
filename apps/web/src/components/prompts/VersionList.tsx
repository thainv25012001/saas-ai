import { Badge } from "@/components/ui/Badge";
import { cn, focusRing } from "@/components/ui/cn";
import { formatTimestamp } from "@/lib/format";

export type VersionRow = {
  id: string;
  version: number;
  systemPrompt: string;
  isActive: boolean;
  notes: string | null;
  createdAt: string;
};

export function VersionList({
  versions,
  selectedId,
  onSelect,
}: {
  versions: readonly VersionRow[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <ul className="space-y-1 p-2">
      {versions.map((version) => (
        <li key={version.id}>
          <button
            type="button"
            aria-pressed={version.id === selectedId}
            onClick={() => onSelect(version.id)}
            className={cn(
              "w-full rounded-control px-3 py-2 text-left hover:bg-surface-muted",
              version.id === selectedId && "bg-surface-muted",
              focusRing,
              "focus-visible:ring-offset-1",
            )}
          >
            <span className="flex items-center gap-2">
              <span className="text-sm font-medium text-ink">{`v${version.version}`}</span>
              {version.isActive ? <Badge tone="success">Active</Badge> : null}
              <span className="ml-auto text-xs text-ink-subtle">{formatTimestamp(version.createdAt)}</span>
            </span>
            <span className="mt-0.5 block truncate text-xs text-ink-muted">{version.notes ?? "No notes"}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}
