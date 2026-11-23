/**
 * A small parser for server-sent events (the text/event-stream format).
 *
 * The browser's built-in EventSource can't send an Authorization header or a POST body, so
 * the app reads the response stream with fetch() and parses it here. Network chunks don't line
 * up with events, so the parser keeps the unfinished tail between calls to push().
 */
export type SseEvent = { event: string; data: string };

export class SseParser {
  private buffer = "";

  /** Feeds more text; returns every event that is now complete. */
  push(text: string): SseEvent[] {
    this.buffer += text.replace(/\r\n?/g, "\n");
    const events: SseEvent[] = [];
    let boundary = this.buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const frame = this.buffer.slice(0, boundary);
      this.buffer = this.buffer.slice(boundary + 2);
      const parsed = parseFrame(frame);
      if (parsed) events.push(parsed);
      boundary = this.buffer.indexOf("\n\n");
    }
    return events;
  }
}

function parseFrame(frame: string): SseEvent | null {
  let event = "message";
  const data: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith(":")) continue; // comment / keep-alive
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }
  return data.length ? { event, data: data.join("\n") } : null;
}
