import { describe, expect, it } from "vitest";

import { SseParser } from "./sse";

describe("SseParser", () => {
  it("parses complete events", () => {
    const parser = new SseParser();
    expect(parser.push('event: delta\ndata: {"text":"Hi"}\n\nevent: done\ndata: {}\n\n')).toEqual([
      { event: "delta", data: '{"text":"Hi"}' },
      { event: "done", data: "{}" },
    ]);
  });

  it("keeps partial frames until they are complete", () => {
    const parser = new SseParser();
    expect(parser.push("event: del")).toEqual([]);
    expect(parser.push('ta\ndata: {"text":')).toEqual([]);
    expect(parser.push('"x"}\n\n')).toEqual([{ event: "delta", data: '{"text":"x"}' }]);
  });

  it("handles CRLF line endings, comments and multi-line data", () => {
    const parser = new SseParser();
    expect(parser.push(": keep-alive\r\n\r\ndata: line 1\r\ndata: line 2\r\n\r\n")).toEqual([
      { event: "message", data: "line 1\nline 2" },
    ]);
  });
});
