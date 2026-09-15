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

export default function RegisterPage() {
  const { register } = useAuth();
  const router = useRouter();
  const [fullName, setFullName] = useState("");
  const [organizationName, setOrganizationName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await register({
        email,
        password,
        full_name: fullName,
        organization_name: organizationName,
      });
      router.push("/dashboard");
    } catch (caught) {
      setError((caught as { message?: string }).message ?? "Registration failed");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card>
      <CardBody className="space-y-5 p-6">
        <div>
          <h2 className="text-base font-semibold text-ink">Create your workspace</h2>
          <p className="mt-0.5 text-sm text-ink-muted">
            One workspace per business. You can invite people later.
          </p>
        </div>

        {error ? <Alert tone="danger">{error}</Alert> : null}

        <form onSubmit={onSubmit} className="space-y-4">
          <Field label="Full name" required>
            {(control) => (
              <Input
                {...control}
                type="text"
                autoComplete="name"
                value={fullName}
                onChange={(e) => setFullName(e.target.value)}
              />
            )}
          </Field>

          <Field
            label="Organization name"
            description="Shown to your team. Your agents and data belong to it."
            required
          >
            {(control) => (
              <Input
                {...control}
                type="text"
                autoComplete="organization"
                value={organizationName}
                onChange={(e) => setOrganizationName(e.target.value)}
              />
            )}
          </Field>

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

          {/* The form has always enforced minLength={12} and never said so,
           * so the only way to learn it was to be rejected. */}
          <Field label="Password" description="At least 12 characters." required>
            {(control) => (
              <Input
                {...control}
                type="password"
                autoComplete="new-password"
                minLength={12}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            )}
          </Field>

          <Button
            type="submit"
            loading={submitting}
            loadingLabel="Creating…"
            className="w-full"
          >
            Create workspace
          </Button>
        </form>

        <p className="text-center text-sm text-ink-muted">
          Already have an account?{" "}
          <Link href="/login" className="font-medium text-ink underline">
            Sign in
          </Link>
        </p>
      </CardBody>
    </Card>
  );
}
