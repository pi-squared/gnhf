import { createHash } from "node:crypto";

if (!process.argv.includes("-"))
  throw new Error("Expected stdin prompt marker");
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const prompt = Buffer.concat(chunks);
console.log(
  JSON.stringify({
    type: "turn.completed",
    usage: { input_tokens: 10, cached_input_tokens: 3, output_tokens: 2 },
  }),
);
console.log(
  JSON.stringify({
    type: "item.completed",
    item: {
      type: "agent_message",
      text: JSON.stringify({
        success: true,
        summary: createHash("sha256").update(prompt).digest("hex"),
        should_fully_stop: false,
      }),
    },
  }),
);
