import type { Metadata } from "next";
import { AuthProvider } from "@/lib/auth";
import { UrqlProvider } from "@/lib/urql";
import "./globals.css";

export const metadata: Metadata = {
  title: "AI Sales Agent",
  description: "Configure and test your AI sales assistant.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      {/* Browser extensions (e.g. Grammarly) inject attributes on <body> before
          React hydrates, which mismatches the server-rendered HTML. */}
      <body
        className="min-h-screen bg-canvas font-sans text-ink antialiased"
        suppressHydrationWarning
      >
        <AuthProvider>
          <UrqlProvider>{children}</UrqlProvider>
        </AuthProvider>
      </body>
    </html>
  );
}
