"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card, CardBody } from "@/components/ui/Card";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";
import { useAuth } from "@/lib/auth";

export default function LoginPage() {
  const { login } = useAuth();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login(email, password);
      router.push("/dashboard");
    } catch (caught) {
      setError((caught as { message?: string }).message ?? "Login failed");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card>
      <CardBody padding="loose" className="space-y-5">
        <div>
          <h2 className="text-base font-semibold text-ink">Sign in</h2>
          <p className="mt-0.5 text-sm text-ink-muted">Welcome back.</p>
        </div>

        {error ? <Alert tone="danger">{error}</Alert> : null}

        <form onSubmit={onSubmit} className="space-y-4">
          <Field label="Email" required>
            {(control) => (
              <Input
                {...control}
                type="email"
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            )}
          </Field>

          <Field label="Password" required>
            {(control) => (
              <Input
                {...control}
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            )}
          </Field>

          <Button
            type="submit"
            loading={submitting}
            loadingLabel="Signing in…"
            className="w-full"
          >
            Sign in
          </Button>
        </form>

        <p className="text-center text-sm text-ink-muted">
          No account?{" "}
          <Link href="/register" className="font-medium text-ink underline">
            Create one
          </Link>
        </p>
      </CardBody>
    </Card>
  );
}
