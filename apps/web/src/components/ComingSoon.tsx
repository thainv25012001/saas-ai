export function ComingSoon({ title, phase }: { title: string; phase: string }) {
  return (
    <section>
      <h1 className="text-2xl font-semibold">{title}</h1>
      <p className="mt-2 max-w-prose text-slate-600">
        Arriving in {phase}. The navigation is in place now so the shape of the
        product is visible while the backend catches up.
      </p>
    </section>
  );
}
