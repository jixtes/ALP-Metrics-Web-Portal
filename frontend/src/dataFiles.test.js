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
