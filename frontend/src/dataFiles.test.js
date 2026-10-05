import assert from "node:assert/strict";
import { test } from "node:test";
import { groupProjectFiles } from "./dataFiles.js";

test("groups V2 processed and raw project folders under the same project", () => {
  const rows = groupProjectFiles([
    {
      pipeline_version: "V2", project_key: "V2:Soufflet 2 (2024)",
      relative_path: "Soufflet 2 (2024)/ALP Retailer Survey/data/final.csv",
      is_project_data: true, status: "uploaded", uploaded_at: "2026-10-05T10:00:00Z",
      folder_web_url: "https://example.test/processed",
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

  assert.equal(rows.length, 1);
  assert.equal(rows[0].file_name, "Soufflet 2 (2024)");
  assert.equal(rows[0].folder, "V2/Soufflet 2 (2024)");
  assert.deepEqual(rows[0].data_folders.map(({ name, kind, url }) => ({ name, kind, url })), [
    { name: "ALP Retailer Survey", kind: "data folder", url: "https://example.test/processed" },
    { name: "ALP Commercial Farmer Survey", kind: "raw file", url: "https://example.test/raw.csv" },
  ]);
});

test("shows a raw-only V2 project and its failed upload status", () => {
  const rows = groupProjectFiles([{
    pipeline_version: "V2", project_key: "V2:ETG Tanzania Baseline (2024)",
    relative_path: "raw_projects/ETG Tanzania Baseline (2024)/ALP Lead Farmer Survey/surveycto_data.csv",
    is_project_data: false, status: "failed", folder_web_url: null,
  }]);

  assert.equal(rows.length, 1);
  assert.equal(rows[0].file_name, "ETG Tanzania Baseline (2024)");
  assert.equal(rows[0].status, "failed");
  assert.deepEqual(rows[0].data_folders, []);
});
