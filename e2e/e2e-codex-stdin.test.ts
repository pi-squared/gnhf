import { createHash } from "node:crypto";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { CodexAgent } from "../src/core/agents/codex.js";

describe("Codex prompt transport", () => {
  it("preserves a prompt larger than an OS argument across a real process", async () => {
    const fixture = fileURLToPath(
      new URL(
        process.platform === "win32"
          ? "./fixtures/mock-codex-stdin.cmd"
          : "./fixtures/mock-codex-stdin",
        import.meta.url,
      ),
    );
    const prompt =
      'Synthetic evidence with "quotes", $variables and Unicode: λ\n'.repeat(
        5_000,
      );
    const result = await new CodexAgent("unused-schema.json", fixture).run(
      prompt,
      tmpdir(),
    );
    expect(result.output.summary).toBe(
      createHash("sha256").update(prompt).digest("hex"),
    );
    expect(result.usage).toMatchObject({
      inputTokens: 7,
      cacheReadTokens: 3,
      outputTokens: 2,
    });
  });
});
