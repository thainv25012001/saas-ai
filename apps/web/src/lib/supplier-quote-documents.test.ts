import { describe, expect, it } from "vitest";
import type { DocumentNode } from "graphql";
import { print } from "graphql";

import {
  ConfirmSupplierMappingDocument,
  CreateSupplierDocument,
  SupplierQuoteOverviewDocument,
  SupplierReportDocument,
  UpdateSupplierDocument,
} from "@/graphql/generated";

const SUPPLIER_DOCUMENTS: Record<string, DocumentNode> = {
  SupplierQuoteOverview: SupplierQuoteOverviewDocument,
  SupplierReport: SupplierReportDocument,
  CreateSupplier: CreateSupplierDocument,
  UpdateSupplier: UpdateSupplierDocument,
  ConfirmSupplierMapping: ConfirmSupplierMappingDocument,
};

describe("supplier quote GraphQL documents", () => {
  it.each(Object.entries(SUPPLIER_DOCUMENTS))(
    "%s is a generated typed document",
    (name, document) => {
      const [definition] = document.definitions;
      expect(definition.kind).toBe("OperationDefinition");
      expect(
        definition.kind === "OperationDefinition" && definition.name?.value,
      ).toBe(name);
    },
  );

  it("never selects token, email body, or attachment content fields", () => {
    const source = Object.values(SUPPLIER_DOCUMENTS).map(print).join("\n");
    for (const forbidden of [
      "refreshToken",
      "accessToken",
      "Ciphertext",
      "body",
      "snippet",
      "attachmentBytes",
      "sampleRows",
    ]) {
      expect(source).not.toContain(forbidden);
    }
  });
});
