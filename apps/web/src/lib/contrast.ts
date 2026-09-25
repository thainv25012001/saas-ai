/**
 * Which text colour is readable on a business's `brand_color` (the widget's
 * header, user bubbles, Send button and launcher). A pale brand colour with
 * white text on it is unreadable, so this picks black or white -- whichever
 * has the higher WCAG 2 contrast ratio against the background.
 *
 * `public/widget.js` inlines the same formula (it has no imports); keep the
 * two in step.
 */

const HEX_COLOR = /^#[0-9a-fA-F]{6}$/;

function channel(hex: string, offset: number): number {
  const value = parseInt(hex.slice(offset, offset + 2), 16) / 255;
  return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
}

/** WCAG 2 relative luminance of a `#rrggbb` colour, 0 (black) to 1 (white). */
export function relativeLuminance(hex: string): number {
  return 0.2126 * channel(hex, 1) + 0.7152 * channel(hex, 3) + 0.0722 * channel(hex, 5);
}

/** `#000000` or `#ffffff`, whichever contrasts more with `hex`. Anything
 * that is not a 6-digit hex gets white, matching the default dark launcher. */
export function readableTextOn(hex: string): "#000000" | "#ffffff" {
  if (!HEX_COLOR.test(hex)) return "#ffffff";
  const luminance = relativeLuminance(hex);
  const onBlack = (luminance + 0.05) / 0.05;
  const onWhite = 1.05 / (luminance + 0.05);
  return onBlack > onWhite ? "#000000" : "#ffffff";
}
