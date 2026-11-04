import assert from "node:assert/strict";
import { test } from "node:test";
import { groupProjectFiles } from "./dataFiles.js";

test("shows one direct data file link per V2 project survey", () => {
  const rows = groupProjectFiles([
    {
      pipeline_version: "V2", project_key: "V2:Soufflet 2 (2024)",
      relative_path: "Soufflet 2 (2024)/ALP Retailer Survey/data/ALP_Farmer_FullProcessedDataWithLabels.csv",
      is_project_data: true, status: "uploaded", uploaded_at: "2026-10-05T10:00:00Z",
      folder_web_url: "https://example.test/processed", web_url: "https://example.test/processed.csv",
    },
    {
      pipeline_version: "V2", project_key: "V2:Soufflet 2 (2024)",
      relative_path: "Soufflet 2 (2024)/ALP Retailer Survey/data/ALP_Retail_FinalScores.csv",
      is_project_data: true, status: "uploaded", web_url: "https://example.test/scores.csv",
    },
    {
      pipeline_version: "V2", project_key: "V2:Soufflet 2 (2024)",
      relative_path: "raw_projects/Soufflet 2 (2024)/ALP Commercial Farmer Survey/surveycto_data.csv",
      is_project_data: false, status: "uploaded", uploaded_at: "2026-10-05T11:00:00Z",
      folder_web_url: "https://example.test/v2-root", web_url: "https://example.test/raw.csv",
    },
    {
      pipeline_version: "V2", relative_path: "live_forms/alp_retailer_survey.csv",
      is_project_data: false, status: "uploaded",
    },
  ]);

  assert.equal(rows.length, 2);
  assert.equal(rows[0].file_name, "Soufflet 2 (2024) · ALP Retailer Survey");
  assert.equal(rows[0].folder, "V2/Soufflet 2 (2024)/ALP Retailer Survey/data");
  assert.equal(rows[0].web_url, "https://example.test/processed.csv");
  assert.equal(rows[1].file_name, "Soufflet 2 (2024) · ALP Commercial Farmer Survey");
  assert.equal(rows[1].folder, "V2");
  assert.equal(rows[1].web_url, "https://example.test/raw.csv");
});

test("shows a raw-only V2 survey and its failed upload status", () => {
  const rows = groupProjectFiles([{
    pipeline_version: "V2", project_key: "V2:ETG Tanzania Baseline (2024)",
    relative_path: "raw_projects/ETG Tanzania Baseline (2024)/ALP Lead Farmer Survey/surveycto_data.csv",
    is_project_data: false, status: "failed", folder_web_url: null,
  }]);

  assert.equal(rows.length, 1);
  assert.equal(rows[0].file_name, "ETG Tanzania Baseline (2024) · ALP Lead Farmer Survey");
  assert.equal(rows[0].status, "failed");
  assert.equal(rows[0].web_url, null);
});

test("prefers the raw export when a project survey has both raw and processed files", () => {
  const rows = groupProjectFiles([
    {
      pipeline_version: "V2", project_key: "V2:Project", is_project_data: true,
      relative_path: "Project/Survey/data/ALP_Farmer_FullProcessedDataWithLabels.csv",
      status: "uploaded", web_url: "https://example.test/processed.csv",
    },
    {
      pipeline_version: "V2", project_key: "V2:Project", is_project_data: false,
      relative_path: "raw_projects/Project/Survey/surveycto_data.csv",
      status: "uploaded", web_url: "https://example.test/raw.csv",
    },
  ]);

  assert.equal(rows.length, 1);
  assert.equal(rows[0].web_url, "https://example.test/raw.csv");
});

test("uses configured labels and keeps shared survey names separate across entity types", () => {
  const label = "Shared survey";
  const rows = groupProjectFiles([
    ...["farmer", "retailer"].map((entity) => ({
      pipeline_version: "V2", project_key: "V2:Source project", is_project_data: true,
      relative_path: `${entity}/${label}/data/${label}__ALP_Farmer_FullProcessedDataWithLabels.csv`,
      web_url: `https://example.test/${entity}.csv`,
    })),
    {
      pipeline_version: "V2", project_key: "V2:Source project", is_project_data: false,
      relative_path: `commercial_farmer/${label}/raw/${label}__surveycto_data.csv`,
      web_url: "https://example.test/commercial.csv",
    },
  ]);
  assert.equal(rows.length, 3);
  assert.equal(new Set(rows.map((row) => row.id)).size, 3);
  assert.deepEqual(rows.map((row) => row.file_name), [label, label, label]);
  assert.deepEqual(rows.map((row) => row.folder), [
    `V2/farmer/${label}/data`, `V2/retailer/${label}/data`, `V2/commercial_farmer/${label}/raw`,
  ]);
  assert.equal(rows[0].web_url, "https://example.test/farmer.csv");
});

test("supports a reprocessed label in a prefixed PO filename", () => {
  const label = "Baseline (Reprocessed)";
  const rows = groupProjectFiles([{
    pipeline_version: "V2", project_key: "V2:Original source project", is_project_data: true,
    relative_path: `po/${label}/data/${label}__ALP_PO_FullProcessedDataWithLabel.csv`,
    web_url: "https://example.test/po.csv",
  }]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].file_name, label);
  assert.equal(rows[0].project_key, "V2:Original source project");
});

test("new raw exports retain failed upload status and diagnostics stay hidden", () => {
  const rows = groupProjectFiles([
    {pipeline_version: "V2", project_key: "V2:Project", is_project_data: false, status: "failed",
      relative_path: "commercial_farmer/Configured label/raw/Configured label__surveycto_data.csv"},
    {pipeline_version: "V2", project_key: "V2:Project", is_project_data: false,
      relative_path: "farmer/Configured label/raw/all/Configured label__surveycto_data.csv"},
  ]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].file_name, "Configured label");
  assert.equal(rows[0].status, "failed");
});
