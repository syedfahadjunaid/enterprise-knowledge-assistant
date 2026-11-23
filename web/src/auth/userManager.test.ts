import { describe, expect, it } from "vitest";

import { safeReturnTo } from "./userManager";

describe("safeReturnTo", () => {
  it("keeps same-site paths", () => {
    expect(safeReturnTo("/documents?x=1")).toBe("/documents?x=1");
  });

  it.each([undefined, 42, "https://evil.example/", "//evil.example", "/\\evil.example", "documents"])(
    "falls back to / for %s",
    (value) => {
      expect(safeReturnTo(value)).toBe("/");
    },
  );
});
