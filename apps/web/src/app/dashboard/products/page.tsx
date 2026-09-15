import { PlaceholderPage } from "@/components/PlaceholderPage";

export default function ProductsPage() {
  return (
    <PlaceholderPage
      title="Products"
      phase="Phase 4"
      icon="product"
      description="The catalogue your assistant can quote from and recommend."
      planned={[
        "Products with prices, descriptions and availability.",
        "A catalogue lookup tool the agent can call mid-answer.",
        "CSV import, so the catalogue is not typed in twice.",
      ]}
    />
  );
}
