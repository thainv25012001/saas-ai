/**
 * A boolean the browser remembers — for UI preferences only.
 *
 * The one place this app uses `localStorage`. The access token deliberately
 * does not live there (see `lib/auth.tsx`: any injected script can read it),
 * and that reasoning does not extend to whether a panel is collapsed. Nothing
 * here is a secret, nothing here is authoritative, and losing it costs one
 * click.
 */

import { useCallback, useEffect, useState } from "react";

const TRUE = "1";
const FALSE = "0";

/**
 * `null` means "never chosen" — distinct from "chose false", or a viewport
 * default could never apply to someone who had simply never touched the
 * control.
 *
 * Every access is guarded: in a private window, with site data blocked, or
 * with storage disabled, reading *throws* rather than returning null, and an
 * unguarded read takes the whole page down.
 */
export function readStoredFlag(key: string): boolean | null {
  try {
    const raw = localStorage.getItem(key);
    if (raw === TRUE) return true;
    if (raw === FALSE) return false;
    return null;
  } catch {
    return null;
  }
}

export function writeStoredFlag(key: string, value: boolean): void {
  try {
    localStorage.setItem(key, value ? TRUE : FALSE);
  } catch {
    // Storage is unavailable. The preference is lost, which costs one click.
  }
}

/**
 * `computeDefault` runs only when nothing has been stored, and runs on the
 * client — so it may read the viewport.
 *
 * The stored value is applied in an effect rather than as the initial state
 * on purpose: the server has no `localStorage`, so reading it during render
 * would make the first client render disagree with the server's and React
 * would report a hydration mismatch. The cost is that a remembered
 * non-default paints once in its default state first.
 */
export function useRememberedFlag(
  key: string,
  computeDefault: () => boolean,
): [boolean, (next: boolean) => void] {
  const [value, setValue] = useState(false);

  useEffect(() => {
    const stored = readStoredFlag(key);
    setValue(stored ?? computeDefault());
    // `computeDefault` is deliberately not a dependency: it is a fresh
    // closure on every render, and depending on it would re-run this effect
    // forever, overwriting the user's own choice each time.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const update = useCallback(
    (next: boolean) => {
      setValue(next);
      writeStoredFlag(key, next);
    },
    [key],
  );

  return [value, update];
}
