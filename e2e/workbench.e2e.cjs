const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const appUrl = process.env.WORKBENCH_URL || "http://127.0.0.1:8000";
const repoRoot = path.resolve(__dirname, "..");
const { chromium } = require(path.join(repoRoot, "frontend", "node_modules", "playwright-core"));
const sourceFile = path.join(repoRoot, "examples", "source_offset_financial_ledger.csv");
const targetFile = path.join(repoRoot, "examples", "target_offset_standardized_example.csv");
const multiSourceFile = path.join(repoRoot, "examples", "source_multisheet_financial_data.xlsx");
const multiTargetFile = path.join(repoRoot, "examples", "target_multisheet_standardized_example.xlsx");
const chromeCandidates = [
  process.env.CHROME_PATH,
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
].filter(Boolean);

function waitForApi(page, suffix, method = "POST", timeout = 240_000) {
  return page.waitForResponse(
    (response) => response.request().method() === method && new URL(response.url()).pathname.endsWith(suffix),
    { timeout },
  );
}

async function clickAndRequireOk(locator, responsePromise, label) {
  await locator.click();
  const response = await responsePromise;
  assert(response.ok(), `${label} failed with HTTP ${response.status()}`);
  return response;
}

async function ensureFieldProposal(page, response, label, retries = 3) {
  const failures = [];
  let current = response;
  for (let attempt = 0; attempt <= retries; attempt += 1) {
    if (current.ok()) return current;
    const body = await current.json().catch(() => ({}));
    failures.push(`${current.status()}: ${body.detail || "unknown provider error"}`);
    if (attempt === retries) break;
    const retryButton = page.getByRole("button", { name: "Retry initial suggestion" });
    await retryButton.waitFor();
    const retryPromise = waitForApi(page, "/proposal");
    await retryButton.click();
    current = await retryPromise;
  }
  assert.fail(`${label} failed after ${retries} UI retries: ${failures.join(" | ")}`);
}

async function clickProviderActionWithRetry(page, locator, suffix, label, attempts = 3) {
  const failures = [];
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    const responsePromise = waitForApi(page, suffix);
    await locator.click();
    const response = await responsePromise;
    if (response.ok()) return response;
    const body = await response.json().catch(() => ({}));
    failures.push(`${response.status()}: ${body.detail || "unknown provider error"}`);
    await page.waitForTimeout(250);
  }
  assert.fail(`${label} failed after ${attempts} visible retries: ${failures.join(" | ")}`);
}

async function acceptEveryCurrentField(page, label) {
  const acceptResponse = waitForApi(page, "/fields/accept", "POST", 60_000);
  await clickAndRequireOk(page.getByRole("button", { name: /Accept all \(/ }), acceptResponse, `${label} bulk acceptance`);
}

(async () => {
  const executablePath = chromeCandidates.find((candidate) => fs.existsSync(candidate));
  assert(executablePath, "Chrome or Edge was not found for the browser test.");

  const browser = await chromium.launch({ executablePath, headless: true });
  const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1440, height: 1050 } });
  const page = await context.newPage();
  const browserErrors = [];
  const recoveredProviderResponses = [];
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(`console: ${message.text()}`);
  });
  page.on("pageerror", (error) => browserErrors.push(`pageerror: ${error.message}`));
  page.on("response", (response) => {
    const pathname = new URL(response.url()).pathname;
    if (!response.ok() && [422, 502].includes(response.status()) && (/\/proposal$/.test(pathname) || /\/revisions$/.test(pathname))) {
      recoveredProviderResponses.push({ status: response.status(), pathname });
    }
  });

  try {
    await page.goto(appUrl, { waitUntil: "networkidle" });
    await page.getByRole("heading", { name: "Upload the source and target example together." }).waitFor();

    const uploads = page.locator('input[type="file"]');
    assert.equal(await uploads.count(), 2, "The intake page must expose separate source and target uploads.");
    await uploads.nth(0).setInputFiles(sourceFile);
    await uploads.nth(1).setInputFiles(targetFile);
    const continueButton = page.getByRole("button", { name: "Continue to table matching" });
    assert(await continueButton.isEnabled(), "Continue should enable only after both files are selected.");

    const initialTableResponse = waitForApi(page, "/table-proposal");
    await continueButton.click();
    const tableResponse = await initialTableResponse;
    assert(tableResponse.ok(), `Automatic table proposal failed with HTTP ${tableResponse.status()}`);
    await page.getByText("1/1 paired", { exact: true }).waitFor();
    await page.getByText(/Detected C4:G11/).waitFor();
    await page.getByText(/Detected B3:G5/).waitFor();
    assert(await page.getByRole("button", { name: "Confirm pairs and review fields" }).isEnabled());

    await page.getByRole("button", { name: /Remove source assignment/ }).click();
    assert(!(await page.getByRole("button", { name: "Confirm pairs and review fields" }).isEnabled()));
    await page.locator(".table-card.draggable").dragTo(page.locator(".pair-drop-card"));
    await page.getByText("1/1 paired", { exact: true }).waitFor();
    assert(await page.getByRole("button", { name: "Confirm pairs and review fields" }).isEnabled());

    await page.getByPlaceholder(/Map the Account Master target/).fill(
      "Keep the financial ledger paired to the standardized target example.",
    );
    const regeneratedTableResponse = waitForApi(page, "/table-proposal");
    await clickAndRequireOk(
      page.getByRole("button", { name: "Regenerate from comment" }),
      regeneratedTableResponse,
      "Comment-based table regeneration",
    );
    await page.getByRole("button", { name: "Regenerate from comment" }).waitFor();
    assert(await page.getByRole("button", { name: "Confirm pairs and review fields" }).isEnabled());

    const confirmResponse = waitForApi(page, "/table-confirm");
    const automaticFieldResponse = waitForApi(page, "/proposal");
    await clickAndRequireOk(
      page.getByRole("button", { name: "Confirm pairs and review fields" }),
      confirmResponse,
      "Table confirmation",
    );
    const fieldResponse = await automaticFieldResponse;
    await ensureFieldProposal(page, fieldResponse, "Automatic CSV field proposal");
    await page.locator(".mapping-card").first().waitFor();
    assert.equal(await page.locator(".mapping-card").count(), 6, "Expected all six target fields to be proposed.");
    assert(!(await page.locator("body").innerText()).includes("awaiting live DeepSeek field proposal"));

    await page.locator(".mapping-card").filter({ hasText: "Customer Id" }).locator(".mapping-card-body").click();
    await page.getByPlaceholder("Describe the field mapping change…").fill(
      "Use CIF directly for customer_id and preserve it as text.",
    );
    await clickProviderActionWithRetry(
      page,
      page.getByRole("button", { name: "Propose change" }),
      "/revisions",
      "Field correction proposal",
    );
    await page.getByRole("dialog").waitFor();
    const acceptRevisionResponse = page.waitForResponse(
      (response) => response.request().method() === "POST" && /\/revisions\/[^/]+\/accept$/.test(new URL(response.url()).pathname),
      { timeout: 240_000 },
    );
    await clickAndRequireOk(
      page.getByRole("button", { name: "Accept and lock rule" }),
      acceptRevisionResponse,
      "Field correction acceptance",
    );
    await page.getByRole("dialog").waitFor({ state: "hidden" });

    const enabledChecks = page.locator('.mapping-checkbox:not(:disabled)');
    assert.equal(await enabledChecks.count(), 5, "One revised field is locked and five remain bulk-reviewable.");
    await page.getByRole("button", { name: "Select all" }).click();
    assert.equal(await page.locator('.mapping-checkbox:checked').count(), 5, "Select all should mark every reviewable field.");
    await page.getByRole("button", { name: "Clear all" }).click();
    assert.equal(await page.locator('.mapping-checkbox:checked').count(), 0, "Clear all should clear the bulk selection.");
    await enabledChecks.nth(0).check();
    await enabledChecks.nth(1).check();
    const acceptSelectedResponse = waitForApi(page, "/fields/accept", "POST", 60_000);
    await clickAndRequireOk(page.getByRole("button", { name: "Accept selected (2)" }), acceptSelectedResponse, "Selected field acceptance");
    const acceptAllResponse = waitForApi(page, "/fields/accept", "POST", 60_000);
    await clickAndRequireOk(page.getByRole("button", { name: /Accept all \(3\)/ }), acceptAllResponse, "Remaining field acceptance");

    const validationResponse = waitForApi(page, "/validate", "POST", 60_000);
    await clickAndRequireOk(
      page.getByRole("button", { name: "Run validation" }),
      validationResponse,
      "Validation",
    );
    await page.getByRole("heading", { name: "This table pair is ready" }).waitFor();
    const saveButton = page.getByRole("button", { name: "Save & review implementation" });
    assert(await saveButton.isEnabled(), "Saving should enable after review and validation.");

    const saveResponse = waitForApi(page, "/publish", "POST", 60_000);
    await clickAndRequireOk(saveButton, saveResponse, "Save implementation");
    await page.getByRole("heading", { name: "Saved mapping and deterministic Pandas code" }).waitFor();
    await page.getByText("JSON definition", { exact: true }).waitFor();
    await page.getByText("Pandas / Python", { exact: true }).waitFor();
    assert.equal(await page.getByRole("dialog").count(), 0, "Implementation should be a page, not a modal.");
    assert.equal(await page.locator('a[download]').count(), 0, "No mapping or code download is shown by default.");
    assert(new URL(page.url()).searchParams.has("implementation"), "Saved implementation URL should be refreshable.");

    const implementationReload = page.waitForResponse(
      (response) => response.request().method() === "GET" && /\/api\/implementations\/[^/]+$/.test(new URL(response.url()).pathname),
      { timeout: 60_000 },
    );
    await page.reload({ waitUntil: "domcontentloaded" });
    assert((await implementationReload).ok(), "Persisted implementation could not be reloaded from the database.");
    await page.getByRole("heading", { name: "Saved mapping and deterministic Pandas code" }).waitFor();

    await page.getByRole("button", { name: "New upload pair" }).click();
    await page.getByRole("heading", { name: "Upload the source and target example together." }).waitFor();
    const multiUploads = page.locator('input[type="file"]');
    await multiUploads.nth(0).setInputFiles(multiSourceFile);
    await multiUploads.nth(1).setInputFiles(multiTargetFile);

    const multiTableResponse = waitForApi(page, "/table-proposal");
    await page.getByRole("button", { name: "Continue to table matching" }).click();
    assert((await multiTableResponse).ok(), "Automatic multi-sheet table proposal failed.");
    await page.getByText("2/2 paired", { exact: true }).waitFor();
    assert.equal(await page.locator(".table-card.draggable").count(), 2, "Expected two source worksheet cards.");
    assert.equal(await page.locator(".pair-drop-card").count(), 2, "Expected two target worksheet cards.");

    while (await page.getByRole("button", { name: /Remove source assignment/ }).count()) {
      await page.getByRole("button", { name: /Remove source assignment/ }).first().click();
    }
    await page.getByText("0/2 paired", { exact: true }).waitFor();
    const multiSourceCards = page.locator(".table-card.draggable");
    const multiTargetCards = page.locator(".pair-drop-card");
    await multiSourceCards.nth(0).dragTo(multiTargetCards.nth(0));
    await multiSourceCards.nth(1).dragTo(multiTargetCards.nth(1));
    await page.getByText("2/2 paired", { exact: true }).waitFor();
    assert.equal(await multiTargetCards.nth(0).locator(".assigned-source strong").textContent(), "Financial Ledger");
    assert.equal(await multiTargetCards.nth(1).locator(".assigned-source strong").textContent(), "Account Register");

    const multiConfirmResponse = waitForApi(page, "/table-confirm");
    const firstPairFieldResponse = waitForApi(page, "/proposal");
    await clickAndRequireOk(
      page.getByRole("button", { name: "Confirm pairs and review fields" }),
      multiConfirmResponse,
      "Multi-sheet table confirmation",
    );
    await ensureFieldProposal(page, await firstPairFieldResponse, "First worksheet field proposal");
    await page.locator(".mapping-card").first().waitFor();
    assert.equal(await page.locator(".mapping-card").count(), 6, "Expected six Standard P&L fields.");
    assert.equal(await page.locator(".pair-switcher > div:nth-child(2) button").count(), 2);
    await acceptEveryCurrentField(page, "First worksheet");
    const firstPairValidation = waitForApi(page, "/validate", "POST", 60_000);
    await clickAndRequireOk(
      page.getByRole("button", { name: "Run validation" }),
      firstPairValidation,
      "First worksheet validation",
    );

    const secondPairActivation = page.waitForResponse(
      (response) => response.request().method() === "POST" && /\/table-mappings\/[^/]+\/activate$/.test(new URL(response.url()).pathname),
      { timeout: 60_000 },
    );
    const secondPairFieldResponse = waitForApi(page, "/proposal");
    await page.locator(".pair-switcher > div:nth-child(2) button").nth(1).click();
    assert((await secondPairActivation).ok(), "Second worksheet activation failed.");
    await ensureFieldProposal(page, await secondPairFieldResponse, "Second worksheet field proposal");
    await page.locator(".mapping-card").first().waitFor();
    assert.equal(await page.locator(".mapping-card").count(), 4, "Expected four Account Master fields.");
    assert(!(await page.locator("body").innerText()).includes("awaiting live DeepSeek field proposal"));
    await acceptEveryCurrentField(page, "Second worksheet");
    const secondPairValidation = waitForApi(page, "/validate", "POST", 60_000);
    await clickAndRequireOk(
      page.getByRole("button", { name: "Run validation" }),
      secondPairValidation,
      "Second worksheet validation",
    );
    await page.getByRole("heading", { name: "This table pair is ready" }).waitFor();

    const multiSaveButton = page.getByRole("button", { name: "Save & review implementation" });
    assert(await multiSaveButton.isEnabled(), "Multi-sheet implementation should be ready to save.");
    const multiSaveResponse = waitForApi(page, "/publish", "POST", 60_000);
    await clickAndRequireOk(multiSaveButton, multiSaveResponse, "Multi-sheet save");
    await page.getByRole("heading", { name: "Saved mapping and deterministic Pandas code" }).waitFor();
    assert.equal(await page.locator(".implementation-pairs button").count(), 2, "Both table implementations should be previewable.");

    const resourceErrors = browserErrors.filter((error) => error.startsWith("console: Failed to load resource:"));
    const runtimeErrors = browserErrors.filter((error) => !error.startsWith("console: Failed to load resource:"));
    assert.deepEqual(runtimeErrors, [], `Browser runtime errors were observed:\n${runtimeErrors.join("\n")}`);
    assert(
      resourceErrors.length <= recoveredProviderResponses.length,
      `Unexpected resource errors were observed:\n${resourceErrors.join("\n")}`,
    );
    console.log(JSON.stringify({
      status: "passed",
      csv: {
        uploads: 2,
        automaticTableProposal: true,
        dragAndDrop: true,
        commentRegeneration: true,
        automaticFieldProposal: true,
        mappedFields: 6,
        fieldCorrection: true,
        validation: "ready",
        headerRange: "C4:G11",
        targetRange: "B3:G5",
        bulkSelection: true,
        implementationPage: true,
        persistedAcrossReload: true,
        downloadsShown: 0,
      },
      multiSheetExcel: {
        sourceSheets: 2,
        targetSheets: 2,
        confirmedPairs: 2,
        dragAndDropAssignments: 2,
        automaticFieldProposals: 2,
        mappedFieldsByPair: [6, 4],
        validation: "ready",
        persistedImplementations: 2,
      },
      browserRuntimeErrors: runtimeErrors.length,
      recoveredProviderResponses: recoveredProviderResponses.length,
    }, null, 2));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
