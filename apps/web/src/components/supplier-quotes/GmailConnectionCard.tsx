import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { formatTimestamp } from "@/lib/format";
import { messageErrorLabel } from "@/lib/supplier-quotes";

export type GmailConnectionView = {
  connected: boolean;
  email?: string | null;
  status?: string | null;
  lastHistorySyncAt?: string | null;
  lastErrorCode?: string | null;
};

/**
 * The connected mailbox. Only an owner may connect or disconnect (the API
 * refuses anyone else), so for a member the controls are absent rather than
 * disabled -- the `McpAccessCard` precedent.
 */
export function GmailConnectionCard({
  connection,
  canManage,
  busy,
  error,
  onConnect,
  onDisconnect,
}: {
  connection: GmailConnectionView;
  canManage: boolean;
  busy: boolean;
  error: string | null;
  onConnect: () => void;
  onDisconnect: () => void;
}) {
  const healthy = connection.status === "connected";
  let action: React.ReactNode = null;
  if (canManage) {
    action = connection.connected ? (
      <Button variant="secondary" size="sm" loading={busy} loadingLabel="Disconnecting…" onClick={onDisconnect}>
        Disconnect
      </Button>
    ) : (
      <Button size="sm" loading={busy} loadingLabel="Opening Google…" onClick={onConnect}>
        Connect Gmail
      </Button>
    );
  }

  return (
    <Card>
      <CardHeader
        title="Gmail"
        description="Read-only access. Only emails that arrive after connecting are analysed."
        actions={action}
      />
      <CardBody className="space-y-2">
        {connection.connected ? (
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium text-ink">{connection.email}</span>
            <Badge tone={healthy ? "success" : "warn"}>{healthy ? "Connected" : "Needs attention"}</Badge>
          </div>
        ) : (
          <p className="text-sm text-ink-muted">Not connected.</p>
        )}
        {connection.connected && connection.lastHistorySyncAt ? (
          <p className="text-xs text-ink-subtle">Last checked {formatTimestamp(connection.lastHistorySyncAt)}</p>
        ) : null}
        {connection.connected && connection.lastErrorCode ? (
          <p className="text-xs text-ink-muted">{messageErrorLabel(connection.lastErrorCode)}</p>
        ) : null}
        {canManage ? null : (
          <p className="text-xs text-ink-subtle">Only the organization owner can connect or disconnect Gmail.</p>
        )}
        {error ? <Alert tone="danger">{error}</Alert> : null}
      </CardBody>
    </Card>
  );
}
