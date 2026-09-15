/** A class value that may be conditionally absent. */
export type ClassValue = string | false | null | undefined;

/**
 * Joins the truthy class names. This is the whole of what `clsx` would give
 * us here, and the repo carries no UI dependencies (see the spec, §2).
 */
export function cn(...parts: ClassValue[]): string {
  return parts.filter(Boolean).join(" ");
}

/**
 * The one focus ring, defined once. §7: nowhere else hand-rolls this.
 * The ring-offset width itself is the one thing that legitimately varies
 * (Button's offset-2 vs Input's tighter offset-1), so it stays out of this
 * constant and is added alongside it, rather than layered on top of it —
 * `cn` only concatenates, so two offset-width classes in one call would
 * silently pick whichever Tailwind emits later (see the className-override
 * fix elsewhere in this branch).
 */
export const focusRing =
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink " +
  "focus-visible:ring-offset-canvas";
