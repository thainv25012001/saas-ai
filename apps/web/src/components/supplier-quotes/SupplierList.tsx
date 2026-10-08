import { useState } from "react";
import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { cn, focusRing } from "@/components/ui/cn";
import { EmptyState } from "@/components/ui/EmptyState";
import { Field } from "@/components/ui/Field";
import { Input, Textarea } from "@/components/ui/Input";
import { parseAddressList } from "@/lib/supplier-quotes";

export type SupplierRow = {
  id: string;
  name: string;
  senderEmails: string[];
  senderDomains: string[];
  isActive: boolean;
};

export type SupplierDraft = Omit<SupplierRow, "id">;

type FormState = { name: string; emails: string; domains: string; isActive: boolean };

const EMPTY_FORM: FormState = { name: "", emails: "", domains: "", isActive: true };

/**
 * Suppliers and the sender rules that recognise their emails. One form both
 * adds and edits, so the two cannot drift; `onSave(null, …)` creates.
 */
export function SupplierList({
  suppliers,
  saving,
  error,
  canManage,
  onSave,
}: {
  suppliers: SupplierRow[];
  saving: boolean;
  error: string | null;
  /** Sender rules decide which mail is trusted; only the owner edits them. */
  canManage: boolean;
  onSave: (id: string | null, draft: SupplierDraft) => void;
}) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);

  function edit(supplier: SupplierRow) {
    setEditingId(supplier.id);
    setForm({
      name: supplier.name,
      emails: supplier.senderEmails.join("\n"),
      domains: supplier.senderDomains.join("\n"),
      isActive: supplier.isActive,
    });
  }

  function reset() {
    setEditingId(null);
    setForm(EMPTY_FORM);
  }

  return (
    <div>
      {suppliers.length === 0 ? (
        <EmptyState
          icon="lead"
          title="No suppliers yet"
          description="Add a supplier with the addresses or domains its quotes come from, so their emails are recognised."
        />
      ) : (
        <ul className="divide-y divide-line">
          {suppliers.map((supplier) => (
            <li key={supplier.id} className="flex flex-wrap items-start gap-3 px-5 py-3">
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium text-ink">{supplier.name}</p>
                <ul className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-ink-muted">
                  {supplier.senderEmails.map((email) => (
                    <li key={email}>{email}</li>
                  ))}
                  {supplier.senderDomains.map((domain) => (
                    <li key={domain}>@{domain}</li>
                  ))}
                </ul>
              </div>
              {supplier.isActive ? null : <Badge>Paused</Badge>}
              {canManage ? (
              <Button
                variant="secondary"
                size="sm"
                aria-label={`Edit ${supplier.name}`}
                onClick={() => edit(supplier)}
              >
                Edit
              </Button>
              ) : null}
            </li>
          ))}
        </ul>
      )}

      {canManage ? (
      <form
        className="space-y-3 border-t border-line px-5 py-4"
        onSubmit={(event) => {
          event.preventDefault();
          const name = form.name.trim();
          if (!name) return;
          onSave(editingId, {
            name,
            senderEmails: parseAddressList(form.emails),
            senderDomains: parseAddressList(form.domains),
            isActive: form.isActive,
          });
        }}
      >
        <h3 className="text-sm font-semibold text-ink">{editingId ? "Edit supplier" : "Add a supplier"}</h3>
        <Field label="Name" required>
          {(control) => (
            <Input
              {...control}
              value={form.name}
              maxLength={255}
              onChange={(event) => setForm({ ...form, name: event.target.value })}
            />
          )}
        </Field>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Sender addresses" description="One per line, e.g. quotes@supplier.com.">
            {(control) => (
              <Textarea
                {...control}
                rows={3}
                value={form.emails}
                onChange={(event) => setForm({ ...form, emails: event.target.value })}
              />
            )}
          </Field>
          <Field label="Sender domains" description="Any address at these domains, e.g. supplier.com.">
            {(control) => (
              <Textarea
                {...control}
                rows={3}
                value={form.domains}
                onChange={(event) => setForm({ ...form, domains: event.target.value })}
              />
            )}
          </Field>
        </div>
        <label className="flex items-center gap-2 text-sm text-ink">
          <input
            type="checkbox"
            checked={form.isActive}
            onChange={(event) => setForm({ ...form, isActive: event.target.checked })}
            className={cn("accent-primary", focusRing, "focus-visible:ring-offset-1")}
          />
          Recognise emails from this supplier
        </label>
        {error ? <Alert tone="danger">{error}</Alert> : null}
        <div className="flex gap-2">
          <Button type="submit" disabled={!form.name.trim()} loading={saving} loadingLabel="Saving…">
            {editingId ? "Save supplier" : "Add supplier"}
          </Button>
          {editingId ? (
            <Button type="button" variant="secondary" onClick={reset}>
              Cancel
            </Button>
          ) : null}
        </div>
      </form>
      ) : (
        <p className="border-t border-line px-5 py-3 text-xs text-ink-subtle">
          Only the organization owner can add or change suppliers.
        </p>
      )}
    </div>
  );
}
