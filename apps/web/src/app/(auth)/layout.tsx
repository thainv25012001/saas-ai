export default function AuthLayout({ children }: { children: React.ReactNode }) {
  return (
    <main className="flex min-h-dvh flex-col items-center justify-center gap-6 px-6 py-12">
      {/* The only place a new user meets the product, and previously a bare
       * form on a white page. One sentence is cheap and orients them. */}
      <div className="text-center">
        <h1 className="text-lg font-semibold tracking-tight text-ink">AI Sales Agent</h1>
        <p className="mx-auto mt-1 max-w-sm text-sm text-ink-muted">
          Configure an AI assistant over your own products and documents, then let it talk to your
          customers.
        </p>
      </div>
      <div className="w-full max-w-sm">{children}</div>
    </main>
  );
}
