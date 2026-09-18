// @vitest-environment happy-dom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { UploadDropzone } from "./UploadDropzone";

function pdf(name = "brochure.pdf") {
  return new File(["%PDF-1.4"], name, { type: "application/pdf" });
}

describe("UploadDropzone", () => {
  it("states the accepted types and size limit before any file is chosen", () => {
    // Requirement 4: this text must be visible on first render, not only
    // after a rejected pick.
    render(<UploadDropzone onUpload={vi.fn()} />);
    expect(screen.getByText(/\.pdf/)).toBeInTheDocument();
    // Not "20 MB": the true usable budget is `20 MB - 1 KiB`, and the
    // displayed figure must floor to a number every file at or under it can
    // actually pass (see `formatByteLimit`) -- "20 MB" would be optimistic.
    expect(screen.getByText(/19\.9 MB/)).toBeInTheDocument();
  });

  it("names the lexical embedder plainly, without alarm", () => {
    render(<UploadDropzone onUpload={vi.fn()} />);
    expect(screen.getByText(/shared wording, not meaning/i)).toBeInTheDocument();
  });

  it("uploads a file dropped onto the zone", () => {
    const onUpload = vi.fn();
    render(<UploadDropzone onUpload={onUpload} />);
    const file = pdf();

    fireEvent.drop(screen.getByRole("button"), { dataTransfer: { files: [file] } });

    expect(onUpload).toHaveBeenCalledWith(file);
  });

  it("uploads a file chosen through the picker", () => {
    const onUpload = vi.fn();
    render(<UploadDropzone onUpload={onUpload} />);
    const file = pdf();
    const input = screen.getByLabelText(/choose a document/i) as HTMLInputElement;

    fireEvent.change(input, { target: { files: [file] } });

    expect(onUpload).toHaveBeenCalledWith(file);
  });

  it("ignores a drop while an upload is already in flight", () => {
    const onUpload = vi.fn();
    render(<UploadDropzone onUpload={onUpload} uploading />);

    fireEvent.drop(screen.getByRole("button"), { dataTransfer: { files: [pdf()] } });

    expect(onUpload).not.toHaveBeenCalled();
  });

  it("shows a loading state instead of the drop prompt while uploading", () => {
    render(<UploadDropzone onUpload={vi.fn()} uploading />);
    expect(screen.getByRole("status")).toHaveTextContent(/uploading/i);
    expect(screen.queryByText(/drag a file here/i)).not.toBeInTheDocument();
  });

  it("shows the upload error's message", () => {
    render(
      <UploadDropzone
        onUpload={vi.fn()}
        error={{ code: "unsupported_document_type", message: "\"application/zip\" is not supported." }}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("is not supported");
  });
});
