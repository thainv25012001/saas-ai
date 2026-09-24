// @vitest-environment happy-dom
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ImportTemplateDownload } from "./ImportTemplateDownload";

const auth = { accessToken: "tok", apiUrl: "http://api.test" };

describe("ImportTemplateDownload", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("offers the sample in every format the import accepts", () => {
    render(<ImportTemplateDownload auth={auth} />);
    for (const label of ["CSV", "Excel", "JSON"]) {
      expect(screen.getByRole("button", { name: `Download sample ${label}` })).toBeInTheDocument();
    }
  });

  it("downloads the chosen format and saves it under the server's file name", async () => {
    const urls: string[] = [];
    vi.stubGlobal("fetch", async (url: RequestInfo | URL) => {
      urls.push(String(url));
      return new Response("xlsx-bytes", {
        status: 200,
        headers: { "Content-Disposition": 'attachment; filename="product-import-sample.xlsx"' },
      });
    });
    URL.createObjectURL = vi.fn(() => "blob:sample");
    URL.revokeObjectURL = vi.fn();
    const saved: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      saved.push(this.download);
    });

    render(<ImportTemplateDownload auth={auth} />);
    fireEvent.click(screen.getByRole("button", { name: "Download sample Excel" }));

    await waitFor(() => expect(saved).toEqual(["product-import-sample.xlsx"]));
    expect(urls).toEqual(["http://api.test/api/v1/products/import/template?format=xlsx"]);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:sample");
  });

  it("says so when the download fails", async () => {
    vi.stubGlobal(
      "fetch",
      async () =>
        new Response(JSON.stringify({ error: { code: "internal_error", message: "Server is down" } }), {
          status: 500,
          headers: { "Content-Type": "application/json" },
        }),
    );

    render(<ImportTemplateDownload auth={auth} />);
    fireEvent.click(screen.getByRole("button", { name: "Download sample CSV" }));

    expect(await screen.findByText("Server is down")).toBeInTheDocument();
  });
});
