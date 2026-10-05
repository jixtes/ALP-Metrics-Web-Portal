export function groupProjectFiles(uploads) {
  const rows = [];
  const projects = new Map();
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
    const processed = Boolean(item.is_project_data) && parts.length >= 4 && parts[2] === "data";
    const rawProject = parts.length === 4 && parts[0] === "raw_projects" && parts[3] === "surveycto_data.csv";
    if (!processed && !rawProject) continue;
    const projectName = rawProject ? parts[1] : parts[0];
    const surveyName = rawProject ? parts[2] : parts[1];
    const projectKey = item.project_key || `V2:${projectName}`;
    let project = projects.get(projectKey);
    if (!project) {
      project = {
        ...item,
        id: `project:${projectKey}`,
        file_name: projectName,
        folder: `V2/${projectName}`,
        web_url: null,
        data_folders: [],
        message: "",
      };
      projects.set(projectKey, project);
      rows.push(project);
    }
    const link = rawProject ? item.web_url : item.folder_web_url;
    if (link && !project.data_folders.some((folder) => folder.url === link)) {
      project.data_folders.push({ name: surveyName, url: link,
        kind: rawProject ? "raw file" : "data folder" });
      project.web_url ||= link;
    }
    if (new Date(item.uploaded_at) > new Date(project.uploaded_at || 0)) {
      project.uploaded_at = item.uploaded_at;
    }
    if (item.status === "failed" || (item.status !== "uploaded" && project.status === "uploaded")) {
      project.status = item.status;
    }
  }
  return rows;
}
