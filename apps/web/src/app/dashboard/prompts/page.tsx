import { PlaceholderPage } from "@/components/PlaceholderPage";

export default function PromptsPage() {
  return (
    <PlaceholderPage
      title="Prompts"
      phase="Phase 2"
      icon="prompt"
      description="The versioned system prompts behind your agents."
      planned={[
        "Edit a prompt and publish a new version.",
        "Version history, with the ability to roll back.",
        "See which agents use which prompt version.",
      ]}
    />
  );
}
