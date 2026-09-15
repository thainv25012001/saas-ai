import { Badge } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import type { IconName } from "@/components/ui/icons";
import { PageHeader } from "@/components/ui/PageHeader";

/**
 * A section the navigation shows before it exists. The old version said only
 * that it was "arriving in Phase N"; saying what will live here is the part
 * that makes the wait informative.
 */
export function PlaceholderPage({
  title,
  phase,
  icon,
  description,
  planned,
}: {
  title: string;
  phase: string;
  icon: IconName;
  description: string;
  planned: string[];
}) {
  return (
    <div className="space-y-4">
      <PageHeader title={title} description={description} meta={<Badge tone="warn">{phase}</Badge>} />
      <Card>
        <EmptyState
          icon={icon}
          title={`${title} arrives in ${phase}`}
          description="The navigation shows this section now so the shape of the product is visible while the backend catches up."
        />
        <div className="border-t border-line px-5 py-4">
          <p className="text-xs font-medium uppercase tracking-wide text-ink-subtle">
            What will live here
          </p>
          <ul className="mt-2 space-y-1.5 text-sm text-ink-muted">
            {planned.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      </Card>
    </div>
  );
}
