import type { Source } from "../api/types";

/**
 * The state of one streamed answer, built up event by event (a reducer, so it is easy to test).
 *
 * The answer is a list of segments. A citation marks the text streamed so far as supported by
 * a source; the next text starts a new segment. That lets the UI put the [1] marker right after
 * the sentence it belongs to.
 */
export type Segment = { text: string; sources: number[] };

export type AnswerState = {
  status: "streaming" | "done" | "error";
  sources: Source[];
  segments: Segment[];
  error?: string;
  model?: string | null;
  noLlmCall?: boolean;
};

export type AnswerEvent =
  | { event: "sources"; data: Source[] }
  | { event: "delta"; data: { text: string } }
  | { event: "citation"; data: { source: number; cited_text: string } }
  | {
      event: "done";
      data: { model: string | null; stop_reason: string | null; no_llm_call: boolean; replacement_text?: string };
    }
  | { event: "error"; data: { message: string } };

export const initialAnswer: AnswerState = { status: "streaming", sources: [], segments: [] };

export function answerReducer(state: AnswerState, action: AnswerEvent): AnswerState {
  switch (action.event) {
    case "sources":
      return { ...state, sources: action.data };
    case "delta": {
      const last = state.segments.at(-1);
      if (last && last.sources.length === 0) {
        return { ...state, segments: [...state.segments.slice(0, -1), { ...last, text: last.text + action.data.text }] };
      }
      return { ...state, segments: [...state.segments, { text: action.data.text, sources: [] }] };
    }
    case "citation": {
      const last = state.segments.at(-1);
      const index = action.data.source;
      if (!last || !state.sources.some((s) => s.index === index)) return state;
      if (last.sources.includes(index)) return state;
      return { ...state, segments: [...state.segments.slice(0, -1), { ...last, sources: [...last.sources, index] }] };
    }
    case "done": {
      // A refusal means the streamed text must not be shown.
      const segments = action.data.replacement_text
        ? [{ text: action.data.replacement_text, sources: [] }]
        : state.segments;
      return { ...state, segments, status: "done", model: action.data.model, noLlmCall: action.data.no_llm_call };
    }
    case "error":
      return { ...state, status: "error", error: action.data.message };
  }
}

/** Sources actually cited in the answer, in order of first citation. */
export function citedSources(state: AnswerState): number[] {
  return [...new Set(state.segments.flatMap((s) => s.sources))];
}
