import { afterEach, describe, expect, it, vi } from "vitest";
import {
  buildMappingInput,
  comparabilityReasonLabel,
  disconnectGmail,
  emptyMappingDraft,
  gmailCallbackNotice,
  mappingProblems,
  parseAddressList,
  profitCell,
  rankLabel,
  reportCounts,
  reprocessSupplierMessage,
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
  const labels = ["Item", "Price", "Currency", "Unit"];

  it("starts from the proposed columns", () => {
    const draft = emptyMappingDraft({
      columns: [
        { field: "PRODUCT_NAME", label: "Item" },
        { field: "UNIT_PRICE", label: "Price" },
      ],
      defaultCurrency: null,
      defaultPurchaseUnit: "each",
    });
    expect(draft.fields.PRODUCT_NAME).toBe("Item");
    expect(draft.fields.UNIT_PRICE).toBe("Price");
    expect(draft.fields.CURRENCY).toBe("");
    expect(draft.defaultPurchaseUnit).toBe("each");
  });

  it("mirrors the server's required-field rules", () => {
    const draft = emptyMappingDraft({ columns: [], defaultCurrency: null, defaultPurchaseUnit: null });
    expect(mappingProblems(draft, labels)).toEqual([
      "Choose the column that holds the unit price.",
      "Choose a product name or supplier SKU column.",
      "Choose a currency column or enter a default currency.",
      "Choose a unit column or enter a default purchase unit.",
    ]);
  });

  it("rejects one column mapped to two fields", () => {
    const draft = emptyMappingDraft({
      columns: [
        { field: "PRODUCT_NAME", label: "Item" },
        { field: "SUPPLIER_SKU", label: "Item" },
        { field: "UNIT_PRICE", label: "Price" },
      ],
      defaultCurrency: "USD",
      defaultPurchaseUnit: "each",
    });
    expect(mappingProblems(draft, labels)).toEqual(["Each column can be used for only one field."]);
  });

  it("builds the mutation input from mapped fields only", () => {
    const draft = emptyMappingDraft({
      columns: [
        { field: "PRODUCT_NAME", label: "Item" },
        { field: "UNIT_PRICE", label: "Price" },
        { field: "CURRENCY", label: "Currency" },
      ],
      defaultCurrency: null,
      defaultPurchaseUnit: null,
    });
    draft.defaultPurchaseUnit = " each ";
    expect(mappingProblems(draft, labels)).toEqual([]);
    expect(buildMappingInput({ worksheetName: "Quote", headerRow: 2 }, draft)).toEqual({
      worksheetName: "Quote",
      headerRow: 2,
      columns: [
        { field: "PRODUCT_NAME", label: "Item" },
        { field: "UNIT_PRICE", label: "Price" },
        { field: "CURRENCY", label: "Currency" },
      ],
      defaultCurrency: null,
      defaultPurchaseUnit: "each",
    });
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
