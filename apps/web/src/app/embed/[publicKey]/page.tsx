import { WidgetChat } from "@/components/widget/WidgetChat";
import { API_URL } from "@/lib/api";

/** `/embed/<publicKey>`: the iframe `public/widget.js` opens. Framing is
 * decided per agent by `middleware.ts`; everything else happens client-side
 * in `WidgetChat`, against the public widget API. */
export default async function EmbedPage({ params }: { params: Promise<{ publicKey: string }> }) {
  const { publicKey } = await params;
  return <WidgetChat apiUrl={API_URL} publicKey={publicKey} />;
}
