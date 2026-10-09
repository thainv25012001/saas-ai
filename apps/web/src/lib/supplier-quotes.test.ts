import { afterEach, describe, expect, it, vi } from "vitest";
import {
  bestProfitText,
  buildMappingBody,
  chooseSheet,
  comparabilityReasonLabel,
  confirmMapping,
  disconnectGmail,
  draftLabels,
  emptyMappingDraft,
  gmailCallbackNotice,
  mappingProblems,
  matchStatusLabel,
  messageErrorLabel,
  parseAddressList,
  profitCell,
  profitStatusLabel,
  rankLabel,
  reportCounts,
  reprocessSupplierMessage,
  sheetChoices,
  sheetKey,
  startGmailConnect,
} from "./supplier-quotes";

const auth = { accessToken: "token-1", apiUrl: "https://api.example.com" };

afterEach(() => {
  vi.restoreAllMocks();
});

describe("profitCell", () => {
  it("shows the profit only when the API computed one", () => {
    expect(
      profitCell({ unitGrossProfit: "2.5000", currency: "USD", profitStatus: "available" }),
    ).toEqual({ kind: "profit", text: expect.stringContaining("2.50") });
  });

  it("says the sale price is missing rather than showing a number", () => {
    expect(
      profitCell({ unitGrossProfit: null, currency: "USD", profitStatus: "missing_sale_price" }),
    ).toEqual({ kind: "status", label: "Missing sale price", tone: "warn" });
  });

  it("labels a currency or unit mismatch as not comparable", () => {
    expect(
      profitCell({ unitGrossProfit: null, currency: "EUR", profitStatus: "currency_mismatch" }),
    ).toMatchObject({ label: "Currency differs from catalogue", tone: "warn" });
    expect(
      profitCell({ unitGrossProfit: null, currency: "USD", profitStatus: "unit_mismatch" }),
    ).toMatchObject({ label: "Unit differs from catalogue" });
  });

  it("still renders a status the dashboard does not know yet", () => {
    expect(
      profitCell({ unitGrossProfit: null, currency: null, profitStatus: "something_new" }),
    ).toMatchObject({ kind: "status", label: "Something new", tone: "neutral" });
  });
});

describe("rankLabel", () => {
  it("names the basis the rank was computed on", () => {
    expect(
      rankLabel({ purchasePriceRank: 1, rankingBasis: "purchase_price", currency: "USD", purchaseUnit: "each" }),
    ).toBe("#1 by purchase price · USD per each");
    expect(
      rankLabel({ purchasePriceRank: 2, rankingBasis: "gross_profit", currency: "USD", purchaseUnit: "box" }),
    ).toBe("#2 by gross profit · USD per box");
  });

  it("does not invent a rank for an incomparable offer", () => {
    expect(
      rankLabel({ purchasePriceRank: null, rankingBasis: "none", currency: null, purchaseUnit: null }),
    ).toBe("Not ranked");
  });
});

describe("comparabilityReasonLabel", () => {
  it("translates known reasons and falls back to readable text", () => {
    expect(comparabilityReasonLabel("missing_quote_currency")).toBe("Quote has no currency");
    expect(comparabilityReasonLabel("brand_new_reason")).toBe("Brand new reason");
  });
});

describe("reportCounts", () => {
  it("reads counts from the summary and tolerates anything else", () => {
    expect(reportCounts({ quote_count: 3, issue_count: 1 })).toEqual({ quotes: 3, issues: 1 });
    expect(reportCounts(null)).toEqual({ quotes: 0, issues: 0 });
    expect(reportCounts({ quote_count: "x" })).toEqual({ quotes: 0, issues: 0 });
  });
});

describe("parseAddressList", () => {
  it("splits on commas, whitespace and newlines and drops duplicates", () => {
    expect(parseAddressList("a@x.com, b@x.com\nA@x.com  ")).toEqual(["a@x.com", "b@x.com"]);
    expect(parseAddressList("   ")).toEqual([]);
  });
});

describe("mapping drafts", () => {
  const proposal = {
    worksheetName: "Cover",
    headerRow: 1,
    columns: [
      { field: "PRODUCT_NAME" as const, label: "Supplier" },
    ],
    columnLabels: ["Supplier", "ACME"],
    sheetOptions: [
      { worksheetName: "Cover", headerRow: 1, labels: ["Supplier", "ACME"] },
      { worksheetName: "Quote", headerRow: 3, labels: ["Item", "Price", "Cur", "UOM"] },
    ],
    defaultCurrency: null,
    defaultPurchaseUnit: "each",
  };

  it("starts on the proposed sheet with the proposed columns", () => {
    const draft = emptyMappingDraft(proposal);
    expect(draft.sheet).toBe(sheetKey(proposal));
    expect(draft.fields.PRODUCT_NAME).toBe("Supplier");
    expect(draft.fields.CURRENCY).toBe("");
    expect(draft.defaultPurchaseUnit).toBe("each");
    expect(draftLabels(draft, proposal)).toEqual(["Supplier", "ACME"]);
  });

  it("offers every header row the file has, the proposed one first", () => {
    expect(sheetChoices(proposal).map(sheetKey)).toEqual([
      sheetKey({ worksheetName: "Cover", headerRow: 1 }),
      sheetKey({ worksheetName: "Quote", headerRow: 3 }),
    ]);
    // A proposal saved before sheet options existed still offers itself.
    expect(sheetChoices({ ...proposal, sheetOptions: [] }).map(sheetKey)).toEqual([
      sheetKey(proposal),
    ]);
  });

  it("moving to another sheet clears columns that belong to the old one", () => {
    const moved = chooseSheet(
      emptyMappingDraft(proposal),
      proposal,
      sheetKey({ worksheetName: "Quote", headerRow: 3 }),
    );
    expect(moved.fields.PRODUCT_NAME).toBe("");
    expect(draftLabels(moved, proposal)).toEqual(["Item", "Price", "Cur", "UOM"]);
  });

  it("mirrors the server's required-field rules", () => {
    const draft = { ...emptyMappingDraft(proposal), defaultPurchaseUnit: "" };
    draft.fields.PRODUCT_NAME = "";
    expect(mappingProblems(draft, ["Item"])).toEqual([
      "Choose the column that holds the unit price.",
      "Choose a product name or supplier SKU column.",
      "Choose a currency column or enter a default currency.",
      "Choose a unit column or enter a default purchase unit.",
    ]);
  });

  it("rejects one column mapped to two fields", () => {
    const draft = emptyMappingDraft(proposal);
    draft.fields.SUPPLIER_SKU = "Supplier";
    draft.fields.UNIT_PRICE = "ACME";
    draft.defaultCurrency = "USD";
    expect(mappingProblems(draft, ["Supplier", "ACME"])).toEqual([
      "Each column can be used for only one field.",
    ]);
  });

  it("builds the REST body for the chosen sheet from mapped fields only", () => {
    let draft = chooseSheet(
      emptyMappingDraft(proposal),
      proposal,
      sheetKey({ worksheetName: "Quote", headerRow: 3 }),
    );
    draft = {
      ...draft,
      fields: { ...draft.fields, PRODUCT_NAME: "Item", UNIT_PRICE: "Price", CURRENCY: "Cur" },
      defaultPurchaseUnit: " each ",
    };
    expect(mappingProblems(draft, draftLabels(draft, proposal))).toEqual([]);
    expect(buildMappingBody(draft, proposal)).toEqual({
      worksheet_name: "Quote",
      header_row: 3,
      columns: { product_name: "Item", unit_price: "Price", currency: "Cur" },
      default_currency: null,
      default_purchase_unit: "each",
    });
  });
});

describe("match and status labels", () => {
  it("names a superseded quote and an unprocessable email", () => {
    expect(profitStatusLabel("superseded")).toEqual({
      label: "Replaced by a newer quote",
      tone: "neutral",
    });
    expect(comparabilityReasonLabel("superseded_by_newer_quote")).toBe(
      "Replaced by this supplier's newer quote",
    );
    expect(messageErrorLabel("processing_failed")).toMatch(/could not be processed/i);
    // Unsure classifications are ignored, so what remains looks like a supplier
    // writing from an address no supplier lists yet.
    expect(messageErrorLabel("supplier_classification_review")).toBe(
      "Looks like a supplier, but this address isn't on any supplier yet. Add it to process the email.",
    );
    expect(matchStatusLabel("suggested")).toEqual({ label: "Suggested match", tone: "info" });
  });

  it("shows a best profit only with its currency", () => {
    expect(bestProfitText({ bestProfit: "3.0000", bestProfitCurrency: "USD" })).toMatch(/3\.00/);
    expect(bestProfitText({ bestProfit: null, bestProfitCurrency: null })).toBeNull();
  });
});

describe("gmailCallbackNotice", () => {
  it("reports a connected mailbox and explains a denied consent", () => {
    expect(gmailCallbackNotice("connected", null)).toEqual({
      tone: "success",
      message: "Gmail is connected. New supplier emails will be analysed as they arrive.",
    });
    expect(gmailCallbackNotice("error", "authorization_denied")?.tone).toBe("danger");
    expect(gmailCallbackNotice("error", "authorization_denied")?.message).toMatch(/declined/i);
    expect(gmailCallbackNotice(null, null)).toBeNull();
  });
});

describe("REST calls", () => {
  it("asks for the consent URL with the Bearer token", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        new Response(JSON.stringify({ authorization_url: "https://accounts.google.com/x" }), {
          status: 200,
        }),
      );
    await expect(startGmailConnect(auth)).resolves.toBe("https://accounts.google.com/x");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("https://api.example.com/api/v1/gmail/oauth/start");
    expect(init?.method).toBe("POST");
    expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer token-1");
  });

  it("confirms a mapping over REST and reports whether it was queued", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        new Response(JSON.stringify({ mapping_id: "map-1", queued: false }), { status: 200 }),
      );
    const body = {
      worksheet_name: "Quote",
      header_row: 3,
      columns: { unit_price: "Price" },
      default_currency: "USD",
      default_purchase_unit: "each",
    };
    await expect(confirmMapping("map-1", body, auth)).resolves.toEqual({ queued: false });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("https://api.example.com/api/v1/gmail/supplier-mappings/map-1/confirm");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual(body);
    expect((init?.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });

  it("disconnects and queues a reprocess, surfacing the API's error", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ error: { code: "validation_error", message: "connect Gmail first" } }), {
          status: 422,
        }),
      );
    await disconnectGmail(auth);
    expect(fetchMock.mock.calls[0][1]?.method).toBe("DELETE");
    await expect(reprocessSupplierMessage("m-1", auth)).rejects.toEqual({
      code: "validation_error",
      message: "connect Gmail first",
    });
    expect(fetchMock.mock.calls[1][0]).toBe(
      "https://api.example.com/api/v1/gmail/supplier-messages/m-1/reprocess",
    );
  });
});
