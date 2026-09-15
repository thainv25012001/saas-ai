"use client";

import { useId } from "react";

/** What a `Field` hands its control. Spread it: `<Input {...control} />`. */
export type FieldControlProps = {
  id: string;
  "aria-describedby": string | undefined;
  "aria-invalid": true | undefined;
  required: boolean | undefined;
};

export type FieldProps = {
  label: string;
  /** Says what the field does, or what a value means. Announced with the label. */
  description?: string;
  error?: string | null;
  required?: boolean;
  children: (control: FieldControlProps) => React.ReactNode;
};

/**
 * Owns the wiring that is easy to forget and invisible when missing: a
 * generated id shared by the label and the control, `aria-describedby`
 * pointing at the description and the error, and `aria-invalid`.
 */
export function Field({ label, description, error, required, children }: FieldProps) {
  const id = useId();
  const descriptionId = description ? `${id}-description` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [descriptionId, errorId].filter(Boolean).join(" ") || undefined;

  return (
    <div className="space-y-1.5">
      <label htmlFor={id} className="block text-sm font-medium text-ink">
        {label}
        {required ? (
          <span aria-hidden className="ml-0.5 text-danger">
            *
          </span>
        ) : null}
      </label>

      {/* Before the control, so it is read after the label and before the value. */}
      {description ? (
        <p id={descriptionId} className="text-xs text-ink-subtle">
          {description}
        </p>
      ) : null}

      {children({
        id,
        "aria-describedby": describedBy,
        "aria-invalid": error ? true : undefined,
        required,
      })}

      {error ? (
        <p id={errorId} role="alert" className="text-xs font-medium text-danger">
          {error}
        </p>
      ) : null}
    </div>
  );
}
