import { PlaceholderPage } from "@/components/PlaceholderPage";

export default function LeadsPage() {
  return (
    <PlaceholderPage
      title="Leads"
      phase="Phase 4"
      icon="lead"
      description="The customers your assistant captured, and what they asked for."
      planned={[
        "Leads captured by the agent during a conversation.",
        "The conversation each lead came from.",
        "Export, and a webhook for your CRM.",
      ]}
    />
  );
}
