// @vitest-environment happy-dom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WidgetCard, type WidgetSettingsData } from "./WidgetCard";

beforeEach(() => {
  // The preview `<iframe>` points at a same-origin path happy-dom would
  // otherwise try to actually fetch (there is no server in this test), which
  // logs a network error to the console for every test that renders it.
  // Matches `widget-loader.test.ts`'s own handling of the same iframe.
  const settings = (window as unknown as { happyDOM: { settings: Record<string, unknown> } })
    .happyDOM.settings;
  settings.disableIframePageLoading = true;
  vi.spyOn(console, "error").mockImplementation(() => {});
});

const PUBLIC_KEY = "pk_abc123def456";

function settings(overrides: Partial<WidgetSettingsData> = {}): WidgetSettingsData {
  return {
    enabled: true,
    allowedOrigins: ["https://example.com", "https://shop.example.com"],
    brandColor: "#2563eb",
    position: "RIGHT",
    title: "Ask us anything",
    dailyMessageCap: 500,
    ...overrides,
  };
}

function baseProps(overrides: Partial<Parameters<typeof WidgetCard>[0]> = {}) {
  return {
    publicKey: PUBLIC_KEY,
    agentStatus: "ACTIVE" as const,
    leadToolEnabled: false,
    canEdit: true,
    settings: settings(),
    onSave: vi.fn().mockResolvedValue(true),
    ...overrides,
  };
}

describe("WidgetCard", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("previews readable text on the brand colour as the owner types", () => {
    render(<WidgetCard {...baseProps()} />);
    const sample = screen.getByTestId("brand-text-sample");
    expect(sample.style.color).toMatch(/^(#ffffff|rgb\(255, 255, 255\))$/);

    fireEvent.change(screen.getByLabelText(/brand colou?r/i), { target: { value: "#fef08a" } });
    expect(sample.style.color).toMatch(/^(#000000|rgb\(0, 0, 0\))$/);
  });

  it("loads settings into the fields", () => {
    render(<WidgetCard {...baseProps()} />);
    expect(screen.getByLabelText(/enable the widget/i)).toBeChecked();
    expect(screen.getByLabelText(/allowed domains/i)).toHaveValue(
      "https://example.com\nhttps://shop.example.com",
    );
    expect(screen.getByLabelText(/brand colou?r/i)).toHaveValue("#2563eb");
    expect(screen.getByLabelText(/position/i)).toHaveValue("RIGHT");
    expect(screen.getByLabelText(/^title/i)).toHaveValue("Ask us anything");
    expect(screen.getByLabelText(/daily message cap/i)).toHaveValue(500);
  });

  it("sends normalized input on save and shows a server error inline", async () => {
    const onSave = vi.fn().mockResolvedValue(false);
    const { rerender } = render(<WidgetCard {...baseProps({ onSave })} />);

    fireEvent.change(screen.getByLabelText(/allowed domains/i), {
      target: { value: "  https://example.com  \n\nhttps://other.example.com,https://third.example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /save widget settings/i }));

    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith({
        enabled: true,
        allowedOrigins: ["https://example.com", "https://other.example.com", "https://third.example.com"],
        brandColor: "#2563eb",
        position: "RIGHT",
        title: "Ask us anything",
        dailyMessageCap: 500,
      }),
    );

    rerender(
      <WidgetCard
        {...baseProps({ onSave, saveError: "'ftp://bad' is not a valid origin: scheme must be https" })}
      />,
    );
    expect(
      screen.getByText("'ftp://bad' is not a valid origin: scheme must be https"),
    ).toBeInTheDocument();
  });

  it("warns when the agent is not active", () => {
    render(<WidgetCard {...baseProps({ agentStatus: "DRAFT" })} />);
    expect(screen.getByText(/until the agent is active/i)).toBeInTheDocument();
  });

  it("does not warn about agent status when the agent is active", () => {
    render(<WidgetCard {...baseProps({ agentStatus: "ACTIVE" })} />);
    expect(screen.queryByText(/until the agent is active/i)).not.toBeInTheDocument();
  });

  it("warns when enabled with no allowed domains", () => {
    render(<WidgetCard {...baseProps({ settings: settings({ enabled: true, allowedOrigins: [] }) })} />);
    expect(screen.getByText(/only the preview will load/i)).toBeInTheDocument();
  });

  it("does not warn about domains when disabled with none set", () => {
    render(<WidgetCard {...baseProps({ settings: settings({ enabled: false, allowedOrigins: [] }) })} />);
    expect(screen.queryByText(/only the preview will load/i)).not.toBeInTheDocument();
  });

  it("warns when the lead tool is granted", () => {
    render(<WidgetCard {...baseProps({ leadToolEnabled: true })} />);
    expect(screen.getByText(/anonymous visitors can submit leads/i)).toBeInTheDocument();
  });

  it("always shows that the domain list does not protect the API itself", () => {
    render(<WidgetCard {...baseProps()} />);
    expect(screen.getByText(/does not stop/i)).toBeInTheDocument();
  });

  it("is read-only with no save button when canEdit is false", () => {
    render(<WidgetCard {...baseProps({ canEdit: false })} />);
    expect(screen.queryByRole("button", { name: /save widget settings/i })).not.toBeInTheDocument();
    expect(screen.getByLabelText(/enable the widget/i)).toBeDisabled();
    expect(screen.getByLabelText(/allowed domains/i)).toBeDisabled();
    expect(screen.getByLabelText(/brand colou?r/i)).toBeDisabled();
    expect(screen.getByLabelText(/position/i)).toBeDisabled();
    expect(screen.getByLabelText(/^title/i)).toBeDisabled();
    expect(screen.getByLabelText(/daily message cap/i)).toBeDisabled();
  });

  it("copies the snippet built from the current origin and the public key", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });

    render(<WidgetCard {...baseProps()} />);
    fireEvent.click(screen.getByRole("button", { name: /copy/i }));

    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(
        `<script src="${window.location.origin}/widget.js" data-key="${PUBLIC_KEY}" async></script>`,
      ),
    );
  });

  it("shows the preview iframe pointed at the embed page when enabled", () => {
    render(<WidgetCard {...baseProps({ settings: settings({ enabled: true }) })} />);
    const frame = screen.getByTitle(/widget preview/i) as HTMLIFrameElement;
    expect(frame.src).toContain(`/embed/${PUBLIC_KEY}`);
  });

  it("hides the preview iframe and explains why when disabled", () => {
    render(<WidgetCard {...baseProps({ settings: settings({ enabled: false }) })} />);
    expect(screen.queryByTitle(/widget preview/i)).not.toBeInTheDocument();
    expect(screen.getByText(/preview appears once/i)).toBeInTheDocument();
  });
});
