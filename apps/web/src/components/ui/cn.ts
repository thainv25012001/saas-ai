/** A class value that may be conditionally absent. */
export type ClassValue = string | false | null | undefined;

/**
 * Joins the truthy class names. This is the whole of what `clsx` would give
 * us here, and the repo carries no UI dependencies (see the spec, §2).
 */
export function cn(...parts: ClassValue[]): string {
  return parts.filter(Boolean).join(" ");
}
