"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "urql";
import { UploadDropzone } from "@/components/knowledge/UploadDropzone";
import { ProductImports } from "@/components/products/ProductImports";
import { ProductsTable } from "@/components/products/ProductsTable";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Input, Select } from "@/components/ui/Input";
import { PageHeader } from "@/components/ui/PageHeader";
import { LoadingState } from "@/components/ui/Spinner";
import {
  ProductCategoriesDocument,
  ProductImportsDocument,
  ProductsDocument,
  type ProductAvailability,
} from "@/graphql/generated";
import { API_URL, type ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { firstGraphQLError } from "@/lib/graphql-errors";
import { availabilityLabel, isTerminalImportStatus, shouldPollImports } from "@/lib/product-status";
import {
  ACCEPTED_IMPORT_TYPES,
  importProducts,
  MAX_IMPORT_BYTES,
  validateImportFile,
} from "@/lib/products";
import { usePollWhile } from "@/lib/use-document-polling";
import { useTabHidden } from "@/lib/use-tab-hidden";

const PAGE_SIZE = 50;
/** Recent imports shown above the list. Enough to see the one just started
 * next to the last few, not an audit log. */
const RECENT_IMPORTS = 5;
const SEARCH_DEBOUNCE_MS = 300;

const AVAILABILITIES: ProductAvailability[] = ["IN_STOCK", "OUT_OF_STOCK", "PREORDER", "DISCONTINUED"];

const IMPORT_NOTE = (
  <>
    One product per row. <span className="font-mono">external_id</span> and{" "}
    <span className="font-mono">name</span> are required; importing the same{" "}
    <span className="font-mono">external_id</span> again updates that product instead of adding a
    second one.
  </>
);

export default function ProductsPage() {
  const { user, accessToken, setAccessToken, loading } = useAuth();
  const paused = loading || !user;

  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("");
  const [availability, setAvailability] = useState<ProductAvailability | "">("");
  const [offset, setOffset] = useState(0);

  // A query per keystroke would be a request per letter; wait for a pause.
  useEffect(() => {
    const timer = setTimeout(() => setSearch(searchInput.trim()), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [searchInput]);

  // Any change to what is being asked for starts again from the first page --
  // page 3 of the old filter is meaningless for the new one.
  useEffect(() => {
    setOffset(0);
  }, [search, category, availability]);

  const [{ data, fetching, error }, refetchProducts] = useQuery({
    query: ProductsDocument,
    variables: {
      search: search || null,
      category: category || null,
      availability: availability || null,
      // One more than a page, so the presence of a next page is known
      // without a separate count query.
      limit: PAGE_SIZE + 1,
      offset,
    },
    pause: paused,
  });
  const [{ data: categoriesData }, refetchCategories] = useQuery({
    query: ProductCategoriesDocument,
    pause: paused,
  });
  const [{ data: importsData, error: importsError }, refetchImports] = useQuery({
    query: ProductImportsDocument,
    variables: { limit: RECENT_IMPORTS },
    pause: paused,
  });

  const imports = useMemo(() => importsData?.productImports ?? [], [importsData]);
  const tabHidden = useTabHidden();

  const pollImports = useCallback(() => {
    refetchImports({ requestPolicy: "network-only" });
  }, [refetchImports]);
  usePollWhile(shouldPollImports(imports, tabHidden), pollImports);

  // A settled import is what changes the catalogue, so re-read the list (and
  // its categories) whenever the set of settled imports changes -- including
  // one that finished before the first poll ever saw it running. Skipped on
  // the first answer: that is the page loading, not an import landing.
  const settledImports = imports
    .filter((record) => isTerminalImportStatus(record.status))
    .map((record) => String(record.id))
    .join(",");
  const previousSettled = useRef<string | null>(null);
  useEffect(() => {
    if (!importsData) return;
    if (previousSettled.current !== null && previousSettled.current !== settledImports) {
      refetchProducts({ requestPolicy: "network-only" });
      refetchCategories({ requestPolicy: "network-only" });
    }
    previousSettled.current = settledImports;
  }, [importsData, settledImports, refetchProducts, refetchCategories]);

  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<ApiError | null>(null);

  async function handleUpload(file: File) {
    if (!accessToken) return;
    const validationError = validateImportFile(file);
    if (validationError) {
      setUploadError(validationError);
      return;
    }
    setUploadError(null);
    setUploading(true);
    try {
      await importProducts(file, { accessToken, apiUrl: API_URL, onAccessToken: setAccessToken });
      refetchImports({ requestPolicy: "network-only" });
    } catch (err) {
      setUploadError(err as ApiError);
    } finally {
      setUploading(false);
    }
  }

  const rows = useMemo(() => data?.products ?? [], [data]);
  const products = useMemo(
    () =>
      rows.slice(0, PAGE_SIZE).map((product) => ({
        id: String(product.id),
        externalId: product.externalId,
        name: product.name,
        description: product.description,
        category: product.category,
        price: product.price,
        currency: product.currency,
        attributes: product.attributes,
        availability: product.availability,
        stockQuantity: product.stockQuantity,
        isActive: product.isActive,
        searchIndex: product.searchIndex,
        updatedAt: String(product.updatedAt),
      })),
    [rows],
  );
  const hasNextPage = rows.length > PAGE_SIZE;
  const filtered = Boolean(search || category || availability);
  const knownCategories = categoriesData?.productCategories ?? [];
  // Keep the selected category an option even after the catalogue stops
  // using it: a <select> whose value matches no option paints its first one
  // ("All categories") while the list is still filtered by the old value.
  const categories =
    category && !knownCategories.includes(category) ? [category, ...knownCategories] : knownCategories;
  const queryError = firstGraphQLError(error);
  const importsQueryError = firstGraphQLError(importsError);

  return (
    <div className="space-y-4">
      <PageHeader
        title="Products"
        description="The catalogue your assistant quotes prices from and recommends."
      />

      <Card>
        <div className="p-5">
          <UploadDropzone
            uploading={uploading}
            error={uploadError}
            onUpload={handleUpload}
            acceptedTypes={ACCEPTED_IMPORT_TYPES}
            maxBytes={MAX_IMPORT_BYTES}
            pickLabel="Choose a catalogue file to import"
            note={IMPORT_NOTE}
          />
        </div>
        {importsQueryError ? (
          <div className="px-5 pb-5">
            <Alert tone="danger">{importsQueryError}</Alert>
          </div>
        ) : imports.length > 0 ? (
          <div className="border-t border-line">
            <ProductImports
              imports={imports.map((record) => ({
                id: String(record.id),
                filename: record.filename,
                status: record.status,
                totalRows: record.totalRows,
                succeededCount: record.succeededCount,
                failedCount: record.failedCount,
                error: record.error,
                createdAt: String(record.createdAt),
                errors: record.errors,
              }))}
            />
          </div>
        ) : null}
      </Card>

      {queryError ? <Alert tone="danger">{queryError}</Alert> : null}

      <Card>
        <div className="flex flex-wrap items-center gap-2 border-b border-line px-5 py-3">
          <Input
            type="search"
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
            placeholder="Search by name or SKU"
            aria-label="Search products"
            className="min-w-56 flex-1"
          />
          <Select
            value={category}
            onChange={(event) => setCategory(event.target.value)}
            aria-label="Filter by category"
            width="auto"
            className="min-w-44"
          >
            <option value="">All categories</option>
            {categories.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </Select>
          <Select
            value={availability}
            onChange={(event) => setAvailability(event.target.value as ProductAvailability | "")}
            aria-label="Filter by availability"
            width="auto"
            className="min-w-44"
          >
            <option value="">Any availability</option>
            {AVAILABILITIES.map((value) => (
              <option key={value} value={value}>
                {availabilityLabel(value)}
              </option>
            ))}
          </Select>
        </div>

        {fetching && !data ? (
          <LoadingState label="Loading products…" />
        ) : queryError ? null : (
          <ProductsTable products={products} filtered={filtered} />
        )}

        {offset > 0 || hasNextPage ? (
          <div className="flex items-center justify-between gap-2 border-t border-line px-5 py-3">
            <span className="text-xs text-ink-subtle">
              {offset + 1}–{offset + products.length}
            </span>
            <div className="flex gap-2">
              <Button
                variant="secondary"
                size="sm"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              >
                Previous
              </Button>
              <Button
                variant="secondary"
                size="sm"
                disabled={!hasNextPage}
                onClick={() => setOffset(offset + PAGE_SIZE)}
              >
                Next
              </Button>
            </div>
          </div>
        ) : null}
      </Card>
    </div>
  );
}
