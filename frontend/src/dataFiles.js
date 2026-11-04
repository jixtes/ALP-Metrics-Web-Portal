export function groupProjectFiles(uploads) {
  const rows = [];
  const surveys = new Map();
  for (const item of uploads) {
    const folders = (item.sharepoint_path || item.relative_path || item.local_path || "")
      .replaceAll("\\", "/").split("/").slice(0, -1);
    if (folders.some((folder) => ["qc", "individual_reports"].includes(
      folder.trim().toLowerCase().replace(/[ -]+/g, "_"),
    ))) continue;
    if (item.pipeline_version !== "V2") {
      rows.push(item);
      continue;
    }
    const parts = (item.relative_path || "").split("/");
    const entityScoped = parts.length === 4
      && ["farmer", "retailer", "po", "sme", "commercial_farmer"].includes(parts[0])
      && parts[3].startsWith(`${parts[1]}__`);
    const processed = Boolean(item.is_project_data) && parts.length === 4
      && parts[2] === "data" && /fullprocesseddatawithlabels?\.csv$/i.test(parts[3]);
    const rawProject = parts.length === 4 && (
      (parts[0] === "raw_projects" && parts[3] === "surveycto_data.csv")
      || (entityScoped && parts[2] === "raw" && parts[3] === `${parts[1]}__surveycto_data.csv`)
    );
    if (!processed && !rawProject) continue;
    const projectName = entityScoped ? (item.project_key || "").replace(/^V2:/, "") : rawProject ? parts[1] : parts[0];
    const surveyName = entityScoped ? parts[1] : rawProject ? parts[2] : parts[1];
    const projectKey = item.project_key || `V2:${projectName}`;
    const key = JSON.stringify(entityScoped ? [projectKey, parts[0], surveyName] : [projectKey, surveyName]);
    const row = {
      ...item,
      id: `survey:${key}`,
      file_name: entityScoped ? surveyName : `${projectName} · ${surveyName}`,
      folder: item.folder || (entityScoped ? `V2/${parts.slice(0, -1).join("/")}` : rawProject ? "V2" : `V2/${projectName}/${surveyName}/data`),
      web_url: item.web_url || null,
    };
    const existing = surveys.get(key);
    if (!existing) {
      surveys.set(key, { row, rawProject });
      rows.push(row);
    } else if (rawProject && !existing.rawProject) {
      Object.assign(existing.row, row);
      existing.rawProject = true;
    }
  }
  return rows;
}
