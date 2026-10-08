// @vitest-environment happy-dom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { GmailConnectionCard } from "./GmailConnectionCard";

const connected = {
  connected: true,
  email: "buyer@example.com",
  status: "connected",
  lastHistorySyncAt: "2026-10-01T08:00:00Z",
  lastErrorCode: null,
};

const base = { canManage: true, busy: false, error: null, onConnect: () => {}, onDisconnect: () => {} };

describe("GmailConnectionCard", () => {
  it("offers to connect when no mailbox is connected", () => {
    const onConnect = vi.fn();
    render(<GmailConnectionCard {...base} connection={{ connected: false }} onConnect={onConnect} />);
    expect(screen.getByText(/not connected/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /connect gmail/i }));
    expect(onConnect).toHaveBeenCalled();
  });

  it("names the connected mailbox and disconnects only after confirming", () => {
    const onDisconnect = vi.fn();
    render(<GmailConnectionCard {...base} connection={connected} onDisconnect={onDisconnect} />);
    expect(screen.getByText("buyer@example.com")).toBeInTheDocument();
    expect(screen.getByText("Connected")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^disconnect$/i }));
    expect(onDisconnect).not.toHaveBeenCalled();
    expect(screen.getByText(/reports you already have are kept/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /yes, disconnect/i }));
    expect(onDisconnect).toHaveBeenCalled();
  });

  it("can back out of disconnecting", () => {
    const onDisconnect = vi.fn();
    render(<GmailConnectionCard {...base} connection={connected} onDisconnect={onDisconnect} />);
    fireEvent.click(screen.getByRole("button", { name: /^disconnect$/i }));
    fireEvent.click(screen.getByRole("button", { name: /keep connected/i }));
    expect(screen.getByRole("button", { name: /^disconnect$/i })).toBeInTheDocument();
    expect(onDisconnect).not.toHaveBeenCalled();
  });

  it("surfaces a connection the worker marked as failing", () => {
    render(
      <GmailConnectionCard
        {...base}
        connection={{ ...connected, status: "error", lastErrorCode: "gmail_refresh_failed" }}
      />,
    );
    expect(screen.getByText("Needs attention")).toBeInTheDocument();
    expect(screen.getByText(/reconnect gmail/i)).toBeInTheDocument();
  });

  it("shows a member the status but no controls", () => {
    render(<GmailConnectionCard {...base} canManage={false} connection={connected} />);
    expect(screen.getByText("buyer@example.com")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText(/only the organization owner/i)).toBeInTheDocument();
  });
});
