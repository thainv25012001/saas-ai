import type { FieldControlProps } from "@/components/ui/Field";
import { Input, Select } from "@/components/ui/Input";

export type ModelChoice = {
  id: string;
  label: string;
  contextLength: number | null;
};

type ModelPickerProps = {
  value: string;
  options: readonly ModelChoice[];
  onChange: (modelId: string) => void;
  /** The model list is still in flight. */
  fetching?: boolean;
  /** The model list could not be loaded at all. */
  failed?: boolean;
  // Exactly what `Field` hands its control, not the full element attributes:
  // this renders EITHER a select or an input, and select-typed event handlers
  // do not fit an input.
} & Partial<FieldControlProps>;

function contextLabel(contextLength: number | null): string {
  if (contextLength === null) return "";
  if (contextLength >= 1_000_000) return ` · ${Math.round(contextLength / 1_000_000)}M ctx`;
  return ` · ${Math.round(contextLength / 1000)}k ctx`;
}

/** What the empty slot says, so a blank box is never left to explain itself. */
function placeholderLabel(fetching: boolean, optionCount: number): string {
  if (fetching) return "Loading models…";
  if (optionCount === 0) return "No models for this provider";
  return "Select a model";
}

/**
 * The model field on the agent form.
 *
 * A real `<select>` rather than the free-text input with a `<datalist>` this
 * replaced: a datalist renders nothing until the user types a matching prefix,
 * so the suggestions were invisible unless you already knew what to guess --
 * which defeats the point of suggesting anything.
 */
export function ModelPicker({
  value,
  options,
  onChange,
  fetching = false,
  failed = false,
  ...rest
}: ModelPickerProps) {
  if (failed) {
    // Never leave the field unusable: without the list there is no way to pick
    // a model, and an agent whose model cannot be set cannot be saved.
    return (
      <Input
        {...rest}
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="Model id, e.g. google/gemma-4-31b-it:free"
      />
    );
  }

  // An agent can hold a model the list no longer offers -- a retired free id, a
  // paid one, or anything set before this picker existed. Without this entry
  // the select would show a DIFFERENT model as selected and quietly repoint the
  // agent on the next save.
  const unlisted = value !== "" && !options.some((option) => option.id === value);

  return (
    <Select
      {...rest}
      value={value}
      disabled={fetching}
      onChange={(e) => onChange(e.target.value)}
      // The closed control clips the longer OpenRouter labels. Hover is the
      // only way back to the id they were cut from.
      title={value || undefined}
    >
      {/* A native select with no option matching `value` paints its FIRST one,
       * so an unset model used to display a real model the form was not
       * holding -- and saving sent "". A selected-but-disabled placeholder
       * keeps what is shown and what is saved the same thing. */}
      {value === "" ? (
        <option value="" disabled>
          {placeholderLabel(fetching, options.length)}
        </option>
      ) : null}
      {unlisted ? <option value={value}>{`${value} (not in the current list)`}</option> : null}
      {options.map((option) => (
        <option key={option.id} value={option.id}>
          {`${option.label}${contextLabel(option.contextLength)}`}
        </option>
      ))}
    </Select>
  );
}
