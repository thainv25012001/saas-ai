"use client";

import { useState } from "react";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { cn, focusRing } from "@/components/ui/cn";
import { Field } from "@/components/ui/Field";
import { Icon } from "@/components/ui/icons";
import { Input, Textarea } from "@/components/ui/Input";
import { BUILTIN_TOOL_NAMES, hasExpectation, splitLines } from "@/lib/evaluations";

export type CaseValues = {
  question: string;
  referenceAnswer: string | null;
  requiredPhrases: string[];
  expectedToolNames: string[];
  expectedDocumentIds: string[];
  expectedProductIds: string[];
  tags: string[];
};

export type DocumentOption = { id: string; title: string };
export type ProductOption = { id: string; name: string; externalId: string };

export const EXPECTATION_REQUIRED_MESSAGE =
  "Add at least one expectation — a reference answer, a required phrase, an expected tool, document or product — so the case has something to be scored on.";

const EMPTY: CaseValues = {
  question: "",
  referenceAnswer: null,
  requiredPhrases: [],
  expectedToolNames: [],
  expectedDocumentIds: [],
  expectedProductIds: [],
  tags: [],
};

const checkboxClasses = cn("size-4 rounded-control accent-primary", focusRing, "focus-visible:ring-offset-1");

/**
 * Add or edit one case. Presentational: the page owns the mutations and the
 * product search query (`onProductSearch` is called on every keystroke; the
 * page debounces it, as the Products page does).
 *
 * Mirrors the server's "at least one expectation" rule client-side so the
 * honest path never waits on a 422 -- the server's check is still the one
 * that counts.
 */
export function CaseForm({
  initial,
  documents,
  productOptions,
  productLabels = {},
  onProductSearch,
  onSubmit,
  onCancel,
  submitting = false,
  error = null,
  submitLabel = "Add case",
}: {
  initial?: CaseValues;
  /** Ready documents, to pick expected citations from. */
  documents: readonly DocumentOption[];
  /** The current product search's results. */
  productOptions: readonly ProductOption[];
  /** Display names for product ids already on the case, where known. */
  productLabels?: Readonly<Record<string, string>>;
  onProductSearch: (term: string) => void;
  onSubmit: (values: CaseValues) => Promise<boolean> | boolean;
  onCancel?: () => void;
  submitting?: boolean;
  error?: string | null;
  submitLabel?: string;
}) {
  const start = initial ?? EMPTY;
  const [question, setQuestion] = useState(start.question);
  const [referenceAnswer, setReferenceAnswer] = useState(start.referenceAnswer ?? "");
  const [phrases, setPhrases] = useState(start.requiredPhrases.join("\n"));
  const [tools, setTools] = useState<string[]>(start.expectedToolNames);
  const [documentIds, setDocumentIds] = useState<string[]>(start.expectedDocumentIds);
  const [productIds, setProductIds] = useState<string[]>(start.expectedProductIds);
  const [pickedLabels, setPickedLabels] = useState<Record<string, string>>({});
  const [tags, setTags] = useState(start.tags.join(", "));
  const [productSearch, setProductSearch] = useState("");
  const [questionError, setQuestionError] = useState<string | null>(null);
  const [expectationError, setExpectationError] = useState(false);

  // A document that is no longer ready (deleted, reprocessing) can still be
  // on the case; keep it visible and uncheckable rather than silently lost.
  const knownDocumentIds = new Set(documents.map((doc) => doc.id));
  const documentChoices: DocumentOption[] = [
    ...documents,
    ...documentIds
      .filter((id) => !knownDocumentIds.has(id))
      .map((id) => ({ id, title: `Unavailable document (${id.slice(0, 8)})` })),
  ];

  function toggle(list: string[], value: string, set: (next: string[]) => void) {
    set(list.includes(value) ? list.filter((item) => item !== value) : [...list, value]);
  }

  function productLabel(id: string): string {
    return pickedLabels[id] ?? productLabels[id] ?? `Product ${id.slice(0, 8)}`;
  }

  function values(): CaseValues {
    return {
      question: question.trim(),
      referenceAnswer: referenceAnswer.trim() || null,
      requiredPhrases: splitLines(phrases),
      expectedToolNames: tools,
      expectedDocumentIds: documentIds,
      expectedProductIds: productIds,
      tags: [...new Set(tags.split(",").map((tag) => tag.trim()).filter(Boolean))],
    };
  }

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    const next = values();
    const missingQuestion = next.question === "";
    const missingExpectation = !hasExpectation(next);
    setQuestionError(missingQuestion ? "A case needs a question." : null);
    setExpectationError(missingExpectation);
    if (missingQuestion || missingExpectation) return;
    const saved = await onSubmit(next);
    if (saved && !initial) {
      setQuestion("");
      setReferenceAnswer("");
      setPhrases("");
      setTools([]);
      setDocumentIds([]);
      setProductIds([]);
      setTags("");
      setProductSearch("");
    }
  }

  return (
    <form onSubmit={handleSubmit} noValidate className="space-y-4">
      {error ? <Alert tone="danger">{error}</Alert> : null}

      <Field label="Question" required error={questionError}>
        {(control) => (
          <Textarea
            {...control}
            rows={2}
            maxLength={4000}
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder="What does the extended warranty cover?"
          />
        )}
      </Field>

      <fieldset className="space-y-4 rounded-card border border-line p-4">
        <legend className="px-1 text-sm font-medium text-ink">Expectations</legend>
        <p className="text-xs text-ink-subtle">
          At least one. Each expectation you set becomes a scorer; the case passes when every one of them does.
        </p>

        <Field
          label="Reference answer"
          description="What a correct answer says. Graded by the LLM judge, when a run has one."
        >
          {(control) => (
            <Textarea
              {...control}
              rows={3}
              maxLength={4000}
              value={referenceAnswer}
              onChange={(event) => setReferenceAnswer(event.target.value)}
            />
          )}
        </Field>

        <Field label="Required phrases" description="One per line. The answer must contain each, ignoring case and punctuation.">
          {(control) => (
            <Textarea
              {...control}
              rows={3}
              value={phrases}
              onChange={(event) => setPhrases(event.target.value)}
              placeholder={"3 years\nfree shipping"}
            />
          )}
        </Field>

        <fieldset className="space-y-1.5">
          <legend className="text-sm font-medium text-ink">Expected tools</legend>
          <p className="text-xs text-ink-subtle">Each must be called, successfully. Extra calls do not fail the case.</p>
          <div className="flex flex-wrap gap-x-5 gap-y-2 pt-1">
            {BUILTIN_TOOL_NAMES.map((name) => (
              <label key={name} className="flex items-center gap-2 text-sm text-ink">
                <input
                  type="checkbox"
                  className={checkboxClasses}
                  checked={tools.includes(name)}
                  onChange={() => toggle(tools, name, setTools)}
                />
                <span className="font-mono text-xs">{name}</span>
              </label>
            ))}
          </div>
        </fieldset>

        <fieldset className="space-y-1.5">
          <legend className="text-sm font-medium text-ink">Expected documents</legend>
          <p className="text-xs text-ink-subtle">Each must be cited in the answer.</p>
          {documentChoices.length === 0 ? (
            <p className="text-sm text-ink-muted">No ready documents. Upload one on the Knowledge page.</p>
          ) : (
            <div className="max-h-44 space-y-1.5 overflow-y-auto rounded-control border border-line-strong p-2">
              {documentChoices.map((doc) => (
                <label key={doc.id} className="flex items-center gap-2 text-sm text-ink">
                  <input
                    type="checkbox"
                    className={checkboxClasses}
                    checked={documentIds.includes(doc.id)}
                    onChange={() => toggle(documentIds, doc.id, setDocumentIds)}
                  />
                  <span className="truncate" title={doc.title}>
                    {doc.title}
                  </span>
                </label>
              ))}
            </div>
          )}
        </fieldset>

        <fieldset className="space-y-1.5">
          <legend className="text-sm font-medium text-ink">Expected products</legend>
          <p className="text-xs text-ink-subtle">Each must be cited in the answer.</p>
          {productIds.length > 0 ? (
            <ul className="flex flex-wrap gap-1.5">
              {productIds.map((id) => (
                <li
                  key={id}
                  className="flex items-center gap-1 rounded-control border border-line bg-surface-muted py-0.5 pl-2 pr-1 text-xs text-ink"
                >
                  <span>{productLabel(id)}</span>
                  <button
                    type="button"
                    aria-label={`Remove ${productLabel(id)}`}
                    className={cn("rounded-control p-0.5 text-ink-subtle hover:text-ink", focusRing)}
                    onClick={() => setProductIds(productIds.filter((item) => item !== id))}
                  >
                    <Icon name="close" size="sm" />
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
          <Input
            type="search"
            aria-label="Search products to expect"
            placeholder="Search products by name or SKU"
            value={productSearch}
            onChange={(event) => {
              setProductSearch(event.target.value);
              onProductSearch(event.target.value.trim());
            }}
          />
          {productSearch.trim() !== "" ? (
            productOptions.length === 0 ? (
              <p className="text-xs text-ink-subtle">No products match.</p>
            ) : (
              <ul className="divide-y divide-line rounded-control border border-line">
                {productOptions.map((product) => {
                  const added = productIds.includes(product.id);
                  return (
                    <li key={product.id} className="flex items-center justify-between gap-2 px-3 py-1.5 text-sm">
                      <span className="min-w-0 truncate text-ink">
                        {product.name}{" "}
                        <span className="font-mono text-xs text-ink-subtle">{product.externalId}</span>
                      </span>
                      <Button
                        type="button"
                        size="sm"
                        variant="secondary"
                        disabled={added}
                        onClick={() => {
                          setPickedLabels({ ...pickedLabels, [product.id]: product.name });
                          setProductIds([...productIds, product.id]);
                        }}
                      >
                        {added ? "Added" : "Add"}
                      </Button>
                    </li>
                  );
                })}
              </ul>
            )
          ) : null}
        </fieldset>

        {expectationError ? <Alert tone="danger">{EXPECTATION_REQUIRED_MESSAGE}</Alert> : null}
      </fieldset>

      <Field label="Tags" description="Comma-separated. For your own grouping; not scored.">
        {(control) => (
          <Input {...control} type="text" value={tags} onChange={(event) => setTags(event.target.value)} />
        )}
      </Field>

      <div className="flex items-center gap-3">
        <Button type="submit" loading={submitting} loadingLabel="Saving…">
          {submitLabel}
        </Button>
        {onCancel ? (
          <Button type="button" variant="secondary" onClick={onCancel}>
            Cancel
          </Button>
        ) : null}
      </div>
    </form>
  );
}
