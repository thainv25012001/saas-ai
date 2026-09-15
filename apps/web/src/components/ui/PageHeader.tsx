import Link from "next/link";
import { Icon } from "./icons";

export type Crumb = { href: string; label: string };

export function PageHeader({
  title,
  description,
  meta,
  actions,
  breadcrumb,
}: {
  title: string;
  description?: string;
  /** Small facts that belong beside the title — a slug, a status badge. */
  meta?: React.ReactNode;
  actions?: React.ReactNode;
  breadcrumb?: Crumb[];
}) {
  return (
    <header className="mb-6">
      {breadcrumb?.length ? (
        <nav aria-label="Breadcrumb" className="mb-2 flex items-center gap-1 text-xs text-ink-muted">
          {breadcrumb.map((crumb) => (
            <span key={crumb.href} className="flex items-center gap-1">
              <Link href={crumb.href} className="hover:text-ink hover:underline">
                {crumb.label}
              </Link>
              <Icon name="chevronRight" size="sm" />
            </span>
          ))}
          <span className="text-ink">{title}</span>
        </nav>
      ) : null}

      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold tracking-tight text-ink">{title}</h1>
          {description ? <p className="mt-1 max-w-prose text-sm text-ink-muted">{description}</p> : null}
          {meta ? <div className="mt-2 flex flex-wrap items-center gap-2">{meta}</div> : null}
        </div>
        {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
      </div>
    </header>
  );
}
