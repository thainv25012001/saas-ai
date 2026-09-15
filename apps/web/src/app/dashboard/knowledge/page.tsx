import { PlaceholderPage } from "@/components/PlaceholderPage";

export default function KnowledgePage() {
  return (
    <PlaceholderPage
      title="Knowledge"
      phase="Phase 3"
      icon="knowledge"
      description="The documents your assistant answers from."
      planned={[
        "Upload PDFs, docs and pasted text.",
        "Chunking and embedding, with indexing progress per document.",
        "Retrieval preview: see which chunks an answer drew on.",
      ]}
    />
  );
}
