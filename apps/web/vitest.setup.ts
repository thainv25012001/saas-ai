import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// This setup file runs for every test file, including the `node`-environment
// ones, where there is no document to clean up and `cleanup()` would throw.
afterEach(() => {
  if (typeof document !== "undefined") cleanup();
});
