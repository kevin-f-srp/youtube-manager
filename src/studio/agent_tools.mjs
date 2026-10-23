import { readFile } from "node:fs/promises";
import { spawn } from "node:child_process";
export const name = "studio-editing-tools";
export const inject = ["tools", "attachments"];
export function apply(ctx, config) {
  ctx.tools.register({
    name: "studio",
    description:
      "Operate the current video project. Read project/files, inspect frames, edit shots, inspect/edit/render route maps, render preview, or restore project history. All writes stay in the studio project; originals are read-only. First call help for supported actions.",
    parameters: {
      type: "object",
      properties: {
        action: { type: "string" },
        args: {
          type: "string",
          description: "JSON object of action arguments; use {} for help",
        },
      },
      required: ["action", "args"],
      additionalProperties: false,
    },
    output: {
      schema: { type: "string" },
      render: (_args, value) => {
        const result = JSON.parse(value),
          attachment = result._image_attachment;
        delete result._image_attachment;
        return [
          { type: "text", text: JSON.stringify(result) },
          ...(attachment ? [{ type: "image", attachment }] : []),
        ];
      },
    },
    async execute(args, exec) {
      if (
        !args ||
        typeof args.action !== "string" ||
        typeof args.args !== "string"
      )
        throw Error("action and args are required");
      JSON.parse(args.args);
      const output = await new Promise((resolve, reject) => {
        const child = spawn(
          config.python,
          [
            "-m",
            "src.studio.agent_tools",
            config.folder,
            config.job,
            args.action,
            args.args,
          ],
          { cwd: config.repo, signal: exec.signal },
        );
        let output = "",
          error = "";
        child.stdout.on("data", (b) => {
          output += b;
          if (output.length > 2_000_000) child.kill();
        });
        child.stderr.on("data", (b) => {
          error = (error + b).slice(-2000);
        });
        child.on("error", reject);
        child.on("close", (code) =>
          code === 0
            ? resolve(output)
            : reject(Error(output || "剪辑工具执行失败，请检查工具日志")),
        );
      });
      const result = JSON.parse(output);
      if (result.image) {
        const [attachment] = await ctx.attachments.saveImages([
          { data: await readFile(result.image), mediaType: "image/jpeg" },
        ]);
        result._image_attachment = attachment;
      }
      return JSON.stringify(result);
    },
  });
}
