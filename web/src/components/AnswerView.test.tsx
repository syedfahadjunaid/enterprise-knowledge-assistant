import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { AnswerState } from "../lib/answer";
import { AnswerView } from "./AnswerView";

const answer: AnswerState = {
  status: "done",
  sources: [
    {
      index: 0,
      chunk_id: "c0",
      document_id: "d0",
      document_title: "Company handbook",
      heading: "Remote work",
      page: null,
      text: "You may work from home up to three days per week.",
      score: 0.81,
    },
    {
      index: 1,
      chunk_id: "c1",
      document_id: "d1",
      document_title: "Runbook",
      heading: null,
      page: 2,
      text: "<img src=x onerror=alert(1)>",
      score: 0.62,
    },
  ],
  segments: [
    { text: "Up to three days a week.", sources: [0] },
    { text: " <b>not bold</b>", sources: [] },
  ],
  model: "claude-opus-5-5",
  noLlmCall: false,
};

describe("AnswerView", () => {
  it("shows the answer with citation markers and the sources", () => {
    render(<AnswerView answer={answer} />);
    expect(screen.getByText(/Up to three days a week\./)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Source 1" })).toBeInTheDocument();
    expect(screen.getByText("Company handbook")).toBeInTheDocument();
    expect(screen.getByText("page 2")).toBeInTheDocument();
    expect(screen.getByText("cited")).toBeInTheDocument();
    expect(screen.getByText("retrieved")).toBeInTheDocument();
  });

  it("renders model output and document text as plain text, never as HTML", () => {
    const { container } = render(<AnswerView answer={answer} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
  });

  it("highlights a source when its marker is clicked", async () => {
    render(<AnswerView answer={answer} />);
    await userEvent.click(screen.getByRole("button", { name: "Source 1" }));
    const item = screen.getByText("Company handbook").closest("li");
    expect(item?.className).toContain("bg-indigo-50");
  });
});
