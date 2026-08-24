import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const [input, output] = process.argv.slice(2);
if (!input || !output) throw new Error("usage: render_xlsx_qa.mjs input.xlsx output.png");
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(input));
const preview = await workbook.render({
  sheetName: "项目预算",
  autoCrop: "all",
  scale: 2,
  format: "png",
});
await fs.writeFile(output, new Uint8Array(await preview.arrayBuffer()));

