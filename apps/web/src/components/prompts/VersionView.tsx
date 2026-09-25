"use client";

import { useEffect, useState } from "react";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/Card";
import { activationImpact, activationLabel } from "@/lib/prompts";
import type { VersionRow } from "./VersionList";

export function VersionView({
  version,
  activeVersion,
  agentNames,
  activating,
  error,
  onActivate,
}: {
  version: VersionRow;
  activeVersion: number | null;
  agentNames: readonly string[];
  activating: boolean;
  error: string | null;
  onActivate: (id: string) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  // A confirmation belongs to the version it was asked about.
  useEffect(() => setConfirming(false), [version.id]);

  return (
    <Card>
      <CardHeader
        title={`Version ${version.version}`}
        description={version.isActive ? "Live for every agent using this prompt." : "Not live. Evaluate it, then activate it."}
      />
      <CardBody className="space-y-3">
        {error ? <Alert tone="danger">{error}</Alert> : null}
        <pre className="max-h-[28rem] overflow-auto whitespace-pre-wrap rounded-control bg-surface-muted p-3 font-mono text-xs text-ink">
          {version.systemPrompt}
        </pre>
      </CardBody>
      {version.isActive ? null : (
        <CardFooter className="flex-wrap">
          {confirming ? (
            <>
              <p className="text-sm text-ink-muted">{activationImpact(version.version, agentNames)}</p>
              <Button onClick={() => onActivate(version.id)} loading={activating} loadingLabel="Activating…">
                Confirm
              </Button>
              <Button variant="secondary" onClick={() => setConfirming(false)}>
                Cancel
              </Button>
            </>
          ) : (
            <Button onClick={() => setConfirming(true)}>{activationLabel(version.version, activeVersion)}</Button>
          )}
        </CardFooter>
      )}
    </Card>
  );
}
