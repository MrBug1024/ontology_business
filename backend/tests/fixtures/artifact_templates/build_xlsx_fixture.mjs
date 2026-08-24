import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";
import { fileURLToPath } from "node:url";

const output = fileURLToPath(new URL("./项目预算模板.xlsx", import.meta.url));
const workbook = Workbook.create();
const sheet = workbook.worksheets.add("项目预算");
sheet.showGridLines = false;
sheet.getRange("A1:D1").merge();
sheet.getRange("A1").values = [["{{project.name}}项目预算表"]];
sheet.getRange("A1:D1").format = {
  fill: "#1E40AF",
  font: { bold: true, color: "#FFFFFF", size: 16 },
  horizontalAlignment: "center",
  verticalAlignment: "center",
};
sheet.getRange("A1:D1").format.rowHeight = 30;
sheet.getRange("A3:D6").values = [
  ["项目编号", "{{project.code}}", "负责人", "{{manager.name}}"],
  ["预算金额", "{{metrics.budget}}", "报告日期", "{{report.date}}"],
  ["已发生成本", "{{metrics.actual}}", "当前状态", "{{report.status}}"],
  ["预算余额", null, "完成率", "{{metrics.completion}}%"],
];
sheet.getRange("B6").formulas = [["=B4-B5"]];
sheet.getRange("A3:D6").format.borders = { preset: "all", style: "thin", color: "#CBD5E1" };
sheet.getRange("A3:A6").format = { fill: "#E2E8F0", font: { bold: true } };
sheet.getRange("C3:C6").format = { fill: "#E2E8F0", font: { bold: true } };
sheet.getRange("B4:B6").format.numberFormat = "#,##0.00";
sheet.getRange("A1:D6").format.verticalAlignment = "center";
sheet.getRange("A1:D6").format.wrapText = true;
sheet.getRange("A:D").format.columnWidth = 18;
sheet.freezePanes.freezeRows(1);
const file = await SpreadsheetFile.exportXlsx(workbook);
await file.save(output);
