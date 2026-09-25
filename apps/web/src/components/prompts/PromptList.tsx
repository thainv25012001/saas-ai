import Link from "next/link";
import { Badge } from "@/components/ui/Badge";
import { agentCountLabel } from "@/lib/prompts";

export type PromptRow = {
  id: string;
  name: string;
  key: string;
  activeVersion: number | null;
  versionCount: number;
  agentCount: number;
};

/** Presentational: the page owns the query and maps it to rows. */
export function PromptList({ prompts }: { prompts: readonly PromptRow[] }) {
  return (
    <ul className="divide-y divide-line">
      {prompts.map((prompt) => (
        <li key={prompt.id} className="flex flex-wrap items-center justify-between gap-3 px-5 py-3">
          <div className="min-w-0">
            <Link href={`/dashboard/prompts/${prompt.id}`} className="text-sm font-medium text-ink hover:underline">
              {prompt.name}
            </Link>
            <p className="font-mono text-xs text-ink-subtle">{prompt.key}</p>
          </div>
          <div className="flex shrink-0 items-center gap-3 text-xs text-ink-muted">
            {prompt.activeVersion !== null ? <Badge tone="success">{`v${prompt.activeVersion}`}</Badge> : null}
            <span>{prompt.versionCount === 1 ? "1 version" : `${prompt.versionCount} versions`}</span>
            <span>{agentCountLabel(prompt.agentCount)}</span>
          </div>
        </li>
      ))}
    </ul>
  );
}
