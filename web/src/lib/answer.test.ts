import { describe, expect, it } from "vitest";

import type { Source } from "../api/types";
import { type AnswerEvent, answerReducer, citedSources, initialAnswer } from "./answer";

const source = (index: number): Source => ({
  index,
  chunk_id: `c${index}`,
  document_id: `d${index}`,
  document_title: `Doc ${index}`,
  heading: null,
  page: null,
  text: `Passage ${index}`,
  score: 0.8,
});

const run = (events: AnswerEvent[]) => events.reduce(answerReducer, initialAnswer);

describe("answerReducer", () => {
  it("joins deltas and starts a new segment after a citation", () => {
    const state = run([
      { event: "sources", data: [source(0), source(1)] },
      { event: "delta", data: { text: "Three days " } },
      { event: "delta", data: { text: "a week." } },
      { event: "citation", data: { source: 1, cited_text: "x" } },
      { event: "citation", data: { source: 1, cited_text: "x" } }, // duplicates are ignored
      { event: "delta", data: { text: " Ask HR." } },
    ]);
    expect(state.segments).toEqual([
      { text: "Three days a week.", sources: [1] },
      { text: " Ask HR.", sources: [] },
    ]);
    expect(citedSources(state)).toEqual([1]);
  });

  it("ignores citations of sources that were not sent", () => {
    const state = run([
      { event: "sources", data: [source(0)] },
      { event: "delta", data: { text: "x" } },
      { event: "citation", data: { source: 7, cited_text: "x" } },
    ]);
    expect(state.segments[0]?.sources).toEqual([]);
  });

  it("replaces the streamed text when the model refused", () => {
    const state = run([
      { event: "sources", data: [source(0)] },
      { event: "delta", data: { text: "partial" } },
      {
        event: "done",
        data: { model: "m", stop_reason: "refusal", no_llm_call: false, replacement_text: "I can't help with that request." },
      },
    ]);
    expect(state.segments).toEqual([{ text: "I can't help with that request.", sources: [] }]);
    expect(state.status).toBe("done");
  });

  it("records errors", () => {
    const state = run([{ event: "error", data: { message: "busy" } }]);
    expect(state).toMatchObject({ status: "error", error: "busy" });
  });
});
