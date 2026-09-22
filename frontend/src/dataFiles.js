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
    if (!item.is_project_data || parts[2] !== "data") continue;
    const projectKey = item.project_key || `V2:${parts[0]}`;
    let project = projects.get(projectKey);
    if (!project) {
      project = {
        ...item,
        id: `project:${projectKey}`,
        file_name: parts[0],
        folder: `V2/${parts[0]}`,
        web_url: null,
        data_folders: [],
        message: "",
      };
      projects.set(projectKey, project);
      rows.push(project);
    }
    if (item.folder_web_url && !project.data_folders.some((folder) => folder.url === item.folder_web_url)) {
      project.data_folders.push({ name: parts[1], url: item.folder_web_url });
      project.web_url ||= item.folder_web_url;
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
