"use client";

import { Button } from "@/components/ui/Button";
import { focusRing } from "@/components/ui/cn";
import { Icon } from "@/components/ui/icons";
import { conversationLabel } from "@/lib/conversation-transcript";

export type ConversationSummary = {
  id: string;
  title: string | null;
  preview: string | null;
};

/**
 * The playground's list of past conversations, and the control that gets it
 * out of the way.
 *
 * Collapses to a rail rather than disappearing: the way back stays visible,
 * and "new conversation" stays one click away instead of being hidden behind
 * expanding a panel you did not want open. The rail is the reason this is a
 * component rather than markup in the page -- expanded and collapsed are two
 * renderings of one thing, and keeping them together is what stops them
 * drifting.
 *
 * `New conversation` lives here, not in the page's toolbar: it acts on the
 * thread, and the toolbar is about which agent and model are answering.
 */
export function ConversationPanel({
  conversations,
  currentId,
  fetching,
  collapsed,
  onToggle,
  onOpen,
  onNew,
  canStartNew,
}: {
  conversations: readonly ConversationSummary[];
  /** The conversation on screen, marked in the list. `null` for an unsent
   * new one. */
  currentId: string | null;
  fetching: boolean;
  collapsed: boolean;
  onToggle: () => void;
  onOpen: (id: string) => void;
  onNew: () => void;
  /** False when the transcript is already an empty new conversation, so the
   * action cannot be a no-op that looks like it did something. */
  canStartNew: boolean;
}) {
  const toggleClasses = `rounded-control p-1.5 text-ink-subtle hover:bg-surface-muted hover:text-ink ${focusRing} focus-visible:ring-offset-1`;

  if (collapsed) {
    return (
      <aside className="flex w-12 shrink-0 flex-col items-center gap-1 border-r border-line bg-surface py-3">
        <button
          type="button"
          onClick={onToggle}
          className={toggleClasses}
          aria-label="Show conversations"
          aria-expanded={false}
          title="Show conversations"
        >
          <Icon name="chevronRight" />
        </button>
        <button
          type="button"
          onClick={onNew}
          disabled={!canStartNew}
          className={`${toggleClasses} disabled:pointer-events-none disabled:opacity-40`}
          aria-label="New conversation"
          title="New conversation"
        >
          <Icon name="plus" />
        </button>
      </aside>
    );
  }

  return (
    <aside className="flex w-60 shrink-0 flex-col border-r border-line bg-surface">
      <div className="flex items-center justify-between gap-2 border-b border-line py-2 pl-4 pr-2">
        <h2 className="text-sm font-semibold text-ink">Conversations</h2>
        <button
          type="button"
          onClick={onToggle}
          className={toggleClasses}
          aria-label="Hide conversations"
          aria-expanded
          title="Hide conversations"
        >
          <Icon name="chevronLeft" />
        </button>
      </div>

      <div className="p-2">
        <Button
          variant="secondary"
          size="sm"
          onClick={onNew}
          disabled={!canStartNew}
          className="w-full"
        >
          <Icon name="plus" size="md" />
          New conversation
        </Button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {conversations.length === 0 ? (
          // Loading, empty and populated are three different things to say.
          <p className="px-2 py-3 text-xs text-ink-subtle">
            {fetching ? "Loading…" : "Conversations with this agent are kept here."}
          </p>
        ) : (
          <ul className="space-y-0.5">
            {conversations.map((conversation) => {
              const isOpen = conversation.id === currentId;
              const label = conversationLabel(conversation);
              return (
                <li key={conversation.id}>
                  <button
                    type="button"
                    onClick={() => onOpen(conversation.id)}
                    aria-current={isOpen ? "true" : undefined}
                    // The row is one line and long labels get cut; hover is
                    // the only way back to the whole thing.
                    title={label}
                    className={`w-full truncate rounded-control px-2 py-1.5 text-left text-xs ${focusRing} focus-visible:ring-offset-1 ${
                      isOpen
                        ? "bg-surface-muted font-medium text-ink"
                        : "text-ink-muted hover:bg-surface-muted hover:text-ink"
                    }`}
                  >
                    {/* Interpolated as text, never as markup: both the
                      * generated title and the first-question fallback are
                      * written from what the user typed. */}
                    {label}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </aside>
  );
}
