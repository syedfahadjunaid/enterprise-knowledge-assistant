import { apiRequest } from "../api/client";
import type { AnswerEvent } from "./answer";
import { SseParser } from "./sse";

const KNOWN_EVENTS = new Set(["sources", "delta", "citation", "done", "error"]);

/**
 * POSTs a question and calls `onEvent` for every server-sent event as it arrives.
 * Errors before the stream starts (401, 422, 429, ...) are thrown as ApiError.
 */
export async function streamAnswer(
  question: string,
  onEvent: (event: AnswerEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await apiRequest("/api/ask", {
    method: "POST",
    body: { question },
    signal,
    accept: "text/event-stream",
  });
  if (!response.body) throw new Error("The browser didn't return a readable stream.");

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  const parser = new SseParser();
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    for (const { event, data } of parser.push(value)) {
      if (KNOWN_EVENTS.has(event)) onEvent({ event, data: JSON.parse(data) } as AnswerEvent);
    }
  }
}
