import { Badge } from "@/components/ui/Badge";
import { EmptyState } from "@/components/ui/EmptyState";
import type { ProductAvailability, ProductSearchIndex } from "@/graphql/generated";
import { formatPrice } from "@/lib/format";
import { availabilityLabel, availabilityTone, searchIndexLabel } from "@/lib/product-status";

export type ProductRow = {
  id: string;
  externalId: string;
  name: string;
  description: string | null;
  category: string | null;
  price: string | null;
  currency: string | null;
  attributes: readonly { key: string; value: string }[];
  availability: ProductAvailability;
  stockQuantity: number | null;
  isActive: boolean;
  searchIndex: ProductSearchIndex;
};

export type ProductsTableProps = {
  products: readonly ProductRow[];
  /** Whether a search or filter is narrowing the list -- an empty result then
   * means "nothing matches", not "nothing imported yet". */
  filtered?: boolean;
};

/**
 * The catalogue list. Presentational, like `DocumentsTable`: the page owns
 * the query, the filters and the paging.
 *
 * Every string a product carries -- name, SKU, description, category, and
 * both halves of every attribute -- came from a customer's uploaded file.
 * All of it is rendered as plain JSX text children: never
 * `dangerouslySetInnerHTML`, never a markdown pass (docs/DESIGN.md,
 * "Untrusted text, beyond citations"). React escapes it by construction.
 */
export function ProductsTable({ products, filtered = false }: ProductsTableProps) {
  if (products.length === 0) {
    return filtered ? (
      <EmptyState
        icon="product"
        title="No products match"
        description="Try a different search, or clear the category and availability filters."
      />
    ) : (
      <EmptyState
        icon="product"
        title="No products yet"
        description="Import a CSV or JSON catalogue above. Your assistant can quote prices and recommend products once it lands."
      />
    );
  }

  const anyUnindexed = products.some((product) => product.searchIndex !== "INDEXED");

  return (
    <div>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="border-b border-line bg-surface-muted text-xs uppercase tracking-wide text-ink-subtle">
            <tr>
              <th scope="col" className="px-5 py-2.5 font-medium">
                Product
              </th>
              <th scope="col" className="px-5 py-2.5 font-medium">
                Category
              </th>
              <th scope="col" className="px-5 py-2.5 font-medium">
                Price
              </th>
              <th scope="col" className="px-5 py-2.5 font-medium">
                Availability
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {products.map((product) => {
              const indexLabel = searchIndexLabel(product.searchIndex);
              return (
                <tr key={product.id} className="align-top">
                  <td className="max-w-md px-5 py-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium text-ink">{product.name}</span>
                      {product.isActive ? null : <Badge>Inactive</Badge>}
                      {indexLabel ? <Badge tone="warn">{indexLabel}</Badge> : null}
                    </div>
                    <p className="mt-0.5 font-mono text-xs text-ink-subtle">{product.externalId}</p>
                    {product.description ? (
                      <p className="mt-1 line-clamp-2 text-xs text-ink-muted" title={product.description}>
                        {product.description}
                      </p>
                    ) : null}
                    {product.attributes.length > 0 ? (
                      <dl className="mt-1.5 flex flex-wrap gap-1.5">
                        {product.attributes.map((attribute) => (
                          <div
                            key={attribute.key}
                            className="flex gap-1 rounded-control border border-line bg-surface-muted px-1.5 py-0.5 text-xs"
                          >
                            <dt className="text-ink-subtle">{attribute.key}</dt>
                            <dd className="text-ink">{attribute.value}</dd>
                          </div>
                        ))}
                      </dl>
                    ) : null}
                  </td>
                  <td className="px-5 py-3 text-ink-muted">
                    {product.category ?? <span className="text-ink-subtle">—</span>}
                  </td>
                  <td className="whitespace-nowrap px-5 py-3 text-ink">
                    {formatPrice(product.price, product.currency) ?? (
                      <span className="text-ink-subtle">No price</span>
                    )}
                  </td>
                  <td className="px-5 py-3">
                    <Badge tone={availabilityTone(product.availability)}>
                      {availabilityLabel(product.availability)}
                    </Badge>
                    {product.stockQuantity !== null ? (
                      <p className="mt-1 text-xs text-ink-subtle">{product.stockQuantity} in stock</p>
                    ) : null}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {anyUnindexed ? (
        // Said once under the table rather than in each badge's tooltip: a
        // business owner testing the playground right after an import needs
        // to know why a question "by meaning" misses a product, and nobody
        // hovers a badge to find out.
        <p className="border-t border-line px-5 py-3 text-xs text-ink-subtle">
          Your assistant still finds every product by name, keyword and filters. The products marked
          above are not matched by meaning — “something for a family of five” — until they are
          indexed; importing the same file again does that.
        </p>
      ) : null}
    </div>
  );
}
