import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const repoRoot = path.resolve(import.meta.dirname, "..");
const outputDir = path.join(repoRoot, "examples");
const previewDir = path.join(repoRoot, "backend", "runtime", "e2e-workbook-previews");
const fontFamily = "Arial";

const fixtureDefinitions = [
  {
    filename: "source_multisheet_financial_data.xlsx",
    sheets: [
      {
        name: "Financial Ledger",
        rows: [
          ["CIF", "Report_Date", "Currency", "Line_Item", "Amount"],
          ["NS-1001", "31/12/2025", "SGD", "Revenue", 1250000],
          ["NS-1001", "31/12/2025", "SGD", "Operating Expense", 825000],
          ["NS-1001", "31/12/2025", "SGD", "Net Profit", 425000],
          ["NS-1002", "31/12/2025", "USD", "Revenue", 960000],
          ["NS-1002", "31/12/2025", "USD", "Operating Expense", 610000],
          ["NS-1002", "31/12/2025", "USD", "Net Profit", 350000],
        ],
      },
      {
        name: "Account Register",
        rows: [
          ["Account_Code", "Account_Name", "Category", "Active_Flag"],
          ["4000", "Product revenue", "Revenue", "Y"],
          ["5100", "Operating costs", "Operating Expense", "Y"],
          ["6000", "Net income", "Net Profit", "Y"],
        ],
      },
    ],
  },
  {
    filename: "target_multisheet_standardized_example.xlsx",
    sheets: [
      {
        name: "Standard P&L",
        rows: [
          ["customer_id", "reporting_date", "currency", "total_revenue", "operating_expense", "net_profit"],
          ["EXAMPLE-001", "2025-12-31", "SGD", 1250000, 825000, 425000],
          ["EXAMPLE-002", "2025-12-31", "USD", 960000, 610000, 350000],
        ],
      },
      {
        name: "Account Master",
        rows: [
          ["account_id", "account_label", "account_category", "is_active"],
          ["4000", "Product revenue", "Revenue", "Y"],
          ["5100", "Operating costs", "Operating Expense", "Y"],
        ],
      },
    ],
  },
];

function addSheet(workbook, definition) {
  const sheet = workbook.worksheets.add(definition.name);
  const rowCount = definition.rows.length;
  const columnCount = definition.rows[0].length;
  const used = sheet.getRangeByIndexes(0, 0, rowCount, columnCount);
  used.values = definition.rows;
  used.format.font = { name: fontFamily, size: 10, color: "#172033" };
  used.format.verticalAlignment = "center";
  const header = sheet.getRangeByIndexes(0, 0, 1, columnCount);
  header.format = {
    fill: "#17375E",
    font: { name: fontFamily, size: 10, bold: true, color: "#FFFFFF" },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    borders: { preset: "inside", style: "thin", color: "#FFFFFF" },
  };
  header.format.rowHeight = 24;
  used.format.autofitColumns();
  used.format.autofitRows();
  for (let column = 0; column < columnCount; column += 1) {
    const range = sheet.getRangeByIndexes(0, column, rowCount, 1);
    if (range.format.columnWidth > 28) range.format.columnWidth = 28;
    if (range.format.columnWidth < 12) range.format.columnWidth = 12;
  }
  const numericHeaderIndexes = definition.rows[0]
    .map((value, index) => ({ value, index }))
    .filter(({ value }) => ["Amount", "total_revenue", "operating_expense", "net_profit"].includes(value))
    .map(({ index }) => index);
  for (const column of numericHeaderIndexes) {
    sheet.getRangeByIndexes(1, column, rowCount - 1, 1).format.numberFormat = "#,##0";
  }
  sheet.freezePanes.freezeRows(1);
  sheet.showGridLines = false;
  return sheet;
}

await fs.mkdir(outputDir, { recursive: true });
await fs.mkdir(previewDir, { recursive: true });

const results = [];
for (const fixture of fixtureDefinitions) {
  const workbook = Workbook.create();
  for (const sheetDefinition of fixture.sheets) addSheet(workbook, sheetDefinition);
  workbook.recalculate();

  const checks = [];
  for (const sheetDefinition of fixture.sheets) {
    const lastColumn = String.fromCharCode(64 + sheetDefinition.rows[0].length);
    const range = `${sheetDefinition.name}!A1:${lastColumn}${sheetDefinition.rows.length}`;
    const inspection = await workbook.inspect({
      kind: "table",
      range,
      include: "values,formulas",
      tableMaxRows: 10,
      tableMaxCols: 10,
    });
    checks.push(inspection.ndjson);
    const preview = await workbook.render({
      sheetName: sheetDefinition.name,
      autoCrop: "all",
      scale: 1,
      format: "png",
    });
    const previewName = `${path.parse(fixture.filename).name}-${sheetDefinition.name.replaceAll(" ", "_")}.png`;
    await fs.writeFile(path.join(previewDir, previewName), new Uint8Array(await preview.arrayBuffer()));
  }
  const formulaErrors = await workbook.inspect({
    kind: "match",
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!",
    options: { useRegex: true, maxResults: 50 },
    summary: "fixture formula error scan",
  });
  if (formulaErrors.ndjson.includes('"matchCount":') && !formulaErrors.ndjson.includes('"matchCount":0')) {
    throw new Error(`Formula error found in ${fixture.filename}: ${formulaErrors.ndjson}`);
  }

  const outputPath = path.join(outputDir, fixture.filename);
  const output = await SpreadsheetFile.exportXlsx(workbook);
  await output.save(outputPath);
  const imported = await SpreadsheetFile.importXlsx(await FileBlob.load(outputPath));
  const sheetInspection = await imported.inspect({ kind: "sheet", include: "id,name", maxChars: 3000 });
  for (const sheetDefinition of fixture.sheets) {
    if (!sheetInspection.ndjson.includes(sheetDefinition.name)) {
      throw new Error(`Exported workbook is missing ${sheetDefinition.name}`);
    }
  }
  results.push({ file: outputPath, sheets: fixture.sheets.map((sheet) => sheet.name), checks: checks.length });
}

console.log(JSON.stringify(results, null, 2));
