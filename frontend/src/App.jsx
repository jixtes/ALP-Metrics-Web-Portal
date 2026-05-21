import { useEffect, useRef, useState } from "react";
import alpLogo from "./assets/alp-logo.png";
import ifcLogo from "./assets/ifc-logo.svg";

const RESET_PASSWORD_PATH = "/reset-password";
const POWERBI_SCRIPT_URL = "https://cdn.jsdelivr.net/npm/powerbi-client@2.23.9/dist/powerbi.js";

const emptyDashboard = {
  latest_run: null,
  surveys: [],
  uploads: [],
};

const settingsSections = [
  { key: "profile", label: "My profile", description: "Update your account details and change your password." },
  { key: "pipeline", label: "Pipeline", description: "Update pipeline code and inspect the latest run logs." },
  { key: "users", label: "Manage users", description: "Create users and issue password reset links." },
  { key: "roles", label: "Manage user roles", description: "Create, update, and delete non-admin roles and access rules." },
  { key: "powerbi", label: "Add Power BI dashboards", description: "Choose which reports appear on the landing page." },
];

const surveyColumns = [
  { key: "survey_name", label: "Survey", type: "text" },
  { key: "project_ref", label: "Project", type: "text" },
  { key: "country", label: "Country", type: "text" },
  { key: "client", label: "Client", type: "text" },
  { key: "phase", label: "Phase", type: "text" },
  { key: "submission_count", label: "Submissions", type: "number" },
  { key: "last_submission_at", label: "Latest", type: "date" },
];

const uploadColumns = [
  { key: "file_name", label: "File", type: "text" },
  { key: "folder", label: "Folder", type: "text" },
  { key: "status", label: "Status", type: "text" },
  { key: "web_url", label: "Link", type: "text" },
  { key: "uploaded_at", label: "Uploaded", type: "date" },
];

let powerBIClientPromise;

function formatDate(value) {
  if (!value) {
    return "N/A";
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return new Intl.DateTimeFormat("en-GB", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function formatDateOnly(value) {
  if (!value) {
    return "N/A";
  }

  const date = new Date(`${value}T00:00:00`);
  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return new Intl.DateTimeFormat("en-GB", {
    dateStyle: "medium",
  }).format(date);
}

function formatList(items) {
  return items.length > 0 ? items.join(", ") : "N/A";
}

function compareSurveyValues(left, right, type) {
  if (type === "date") {
    const leftTime = left ? new Date(left).getTime() : Number.NEGATIVE_INFINITY;
    const rightTime = right ? new Date(right).getTime() : Number.NEGATIVE_INFINITY;
    return leftTime - rightTime;
  }

  if (type === "number") {
    return Number(left ?? 0) - Number(right ?? 0);
  }

  return String(left ?? "").localeCompare(String(right ?? ""), undefined, { sensitivity: "base" });
}

function getSharePointFolder(item) {
  const rawPath = item.sharepoint_path || item.local_path || "";
  const normalizedPath = String(rawPath).replaceAll("\\", "/");
  const rootMarker = "alp-metrics-pipeline/";
  const rootIndex = normalizedPath.toLowerCase().indexOf(rootMarker);
  const relativePath =
    rootIndex >= 0 ? normalizedPath.slice(rootIndex + rootMarker.length) : normalizedPath.replace(/^\/+/, "");
  const parts = relativePath.split("/").filter(Boolean);

  if (parts.length <= 1) {
    return ".";
  }

  return parts.slice(0, -1).join("/");
}

function buildEnabledModules(survey) {
  const modules = [];

  if (survey?.trc) {
    modules.push("TRC");
  }
  if (survey?.fpa) {
    modules.push("FPA");
  }
  if (survey?.blr) {
    modules.push("BLR");
  }

  return modules;
}

function getPowerBIModels() {
  return window["powerbi-client"]?.models ?? window.powerbi?.models ?? null;
}

function loadPowerBIClient() {
  if (window.powerbi && getPowerBIModels()) {
    return Promise.resolve(window.powerbi);
  }

  if (!powerBIClientPromise) {
    powerBIClientPromise = new Promise((resolve, reject) => {
      const existingScript = document.querySelector('script[data-powerbi-client="true"]');
      if (existingScript) {
        existingScript.addEventListener("load", () => resolve(window.powerbi), { once: true });
        existingScript.addEventListener("error", () => reject(new Error("Failed to load Power BI client.")), {
          once: true,
        });
        return;
      }

      const script = document.createElement("script");
      script.src = POWERBI_SCRIPT_URL;
      script.async = true;
      script.dataset.powerbiClient = "true";
      script.onload = () => {
        if (window.powerbi && getPowerBIModels()) {
          resolve(window.powerbi);
          return;
        }
        reject(new Error("Power BI client loaded, but the SDK is unavailable."));
      };
      script.onerror = () => reject(new Error("Failed to load Power BI client."));
      document.head.appendChild(script);
    });
  }

  return powerBIClientPromise;
}

function EmbeddedPowerBIReport({ report, onManageDashboards }) {
  const embedContainerRef = useRef(null);
  const [embedError, setEmbedError] = useState("");

  useEffect(() => {
    if (!report || report.error || !embedContainerRef.current) {
      return undefined;
    }

    let activeService;
    let isCancelled = false;

    async function embedReport() {
      const powerbi = await loadPowerBIClient();
      const models = getPowerBIModels();
      if (!models) {
        throw new Error("Power BI SDK models are unavailable in the browser.");
      }
      if (isCancelled || !embedContainerRef.current) {
        return;
      }

      activeService = powerbi;
      setEmbedError("");
      powerbi.reset(embedContainerRef.current);
      powerbi.embed(embedContainerRef.current, {
        type: report.type,
        tokenType: models.TokenType.Embed,
        accessToken: report.accessToken,
        embedUrl: report.embedUrl,
        id: report.reportId,
        settings: {
          layoutType: models.LayoutType.Custom,
          customLayout: {
            displayOption: models.DisplayOption.FitToWidth,
          },
          panes: {
            filters: {
              visible: false,
            },
            pageNavigation: {
              visible: true,
            },
          },
          background: models.BackgroundType.Transparent,
        },
      });
    }

    embedReport().catch((error) => {
      if (!isCancelled) {
        setEmbedError(error.message);
      }
    });

    return () => {
      isCancelled = true;
      if (activeService && embedContainerRef.current) {
        activeService.reset(embedContainerRef.current);
      }
    };
  }, [report]);

  return (
    <article className="detail-card powerbi-card">
      <div className="section-heading section-heading-inline">
        <div>
          <h2>{report.reportName || "Power BI dashboard"}</h2>
        </div>
        <button type="button" className="secondary-button" onClick={onManageDashboards}>
          Manage dashboards
        </button>
      </div>

      {report.error ? <div className="table-empty">{report.error}</div> : null}
      {embedError ? <div className="table-empty">{embedError}</div> : null}
      {!report.error && !embedError ? (
        <div className="powerbi-frame-shell">
          <div className="powerbi-frame" ref={embedContainerRef} />
        </div>
      ) : null}
    </article>
  );
}

function BrandingFooter() {
  return (
    <footer className="brand-footer" aria-label="ALP and IFC footer">
      <div className="brand-footer-inner">
        <img className="ifc-logo" src={ifcLogo} alt="IFC logo" />
        <span className="brand-footer-divider" aria-hidden="true">
          /
        </span>
        <img className="alp-logo" src={alpLogo} alt="Agribusiness Leadership Program logo" />
      </div>
    </footer>
  );
}

function toFriendlyLoginError(error) {
  const message = error instanceof Error ? error.message : String(error ?? "");
  if (!message) {
    return "Unable to sign in right now.";
  }
  if (message.includes("expected pattern")) {
    return "Enter a valid email address.";
  }
  if (message === "Failed to fetch") {
    return "Unable to reach the backend right now.";
  }
  return message;
}

function App() {
  const [routePath, setRoutePath] = useState(window.location.pathname);
  const [dashboard, setDashboard] = useState(emptyDashboard);
  const [selectedSurveyId, setSelectedSurveyId] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isBootstrapping, setIsBootstrapping] = useState(true);
  const [isRunning, setIsRunning] = useState(false);
  const [pipelineStatus, setPipelineStatus] = useState(null);
  const [pipelineOutput, setPipelineOutput] = useState("");
  const [pipelineError, setPipelineError] = useState("");
  const [pipelineMessage, setPipelineMessage] = useState("");
  const [isPipelineStatusLoading, setIsPipelineStatusLoading] = useState(false);
  const [isPullingPipeline, setIsPullingPipeline] = useState(false);
  const [mode, setMode] = useState("surveycto");
  const [error, setError] = useState("");
  const [surveyFilter, setSurveyFilter] = useState("");
  const [uploadFilter, setUploadFilter] = useState("");
  const [sortConfig, setSortConfig] = useState({
    key: "last_submission_at",
    direction: "desc",
  });
  const [uploadSortConfig, setUploadSortConfig] = useState({
    key: "uploaded_at",
    direction: "desc",
  });
  const [csrfToken, setCsrfToken] = useState("");
  const [authUser, setAuthUser] = useState(null);
  const [credentials, setCredentials] = useState({
    email: "",
    password: "",
  });
  const [loginError, setLoginError] = useState("");
  const [currentView, setCurrentView] = useState("dashboard");
  const [activeSettingsSection, setActiveSettingsSection] = useState("profile");
  const [activeDashboardTab, setActiveDashboardTab] = useState("surveys");
  const [availableReports, setAvailableReports] = useState([]);
  const [selectedPowerBIReports, setSelectedPowerBIReports] = useState([]);
  const [savedReportIds, setSavedReportIds] = useState([]);
  const [embeddedReports, setEmbeddedReports] = useState([]);
  const [isPowerBILoading, setIsPowerBILoading] = useState(false);
  const [isSavingPowerBI, setIsSavingPowerBI] = useState(false);
  const [powerBIError, setPowerBIError] = useState("");
  const [powerBIMessage, setPowerBIMessage] = useState("");
  const [profileError, setProfileError] = useState("");
  const [profileMessage, setProfileMessage] = useState("");
  const [isSavingProfile, setIsSavingProfile] = useState(false);
  const [isChangingPassword, setIsChangingPassword] = useState(false);
  const [profileForm, setProfileForm] = useState({
    email: "",
    fullName: "",
  });
  const [passwordChangeForm, setPasswordChangeForm] = useState({
    currentPassword: "",
    newPassword: "",
    confirmPassword: "",
  });
  const [roles, setRoles] = useState([]);
  const [rolesError, setRolesError] = useState("");
  const [rolesMessage, setRolesMessage] = useState("");
  const [roleSearch, setRoleSearch] = useState("");
  const [isRoleFormVisible, setIsRoleFormVisible] = useState(false);
  const [isRolesLoading, setIsRolesLoading] = useState(false);
  const [isSavingRole, setIsSavingRole] = useState(false);
  const [deletingRoleId, setDeletingRoleId] = useState(null);
  const [roleForm, setRoleForm] = useState({
    id: null,
    name: "",
    description: "",
    projectScope: "all",
    allowedProjectRefs: [],
    reportScope: "all",
    allowedReportIds: [],
    uploadScope: "all",
  });
  const [users, setUsers] = useState([]);
  const [usersError, setUsersError] = useState("");
  const [usersMessage, setUsersMessage] = useState("");
  const [userSearch, setUserSearch] = useState("");
  const [isUserFormVisible, setIsUserFormVisible] = useState(false);
  const [isUsersLoading, setIsUsersLoading] = useState(false);
  const [isCreatingUser, setIsCreatingUser] = useState(false);
  const [editingUserId, setEditingUserId] = useState(null);
  const [issuingResetUserId, setIssuingResetUserId] = useState(null);
  const [newUserForm, setNewUserForm] = useState({
    email: "",
    fullName: "",
    role: "viewer",
    password: "",
  });
  const [issuedResetLink, setIssuedResetLink] = useState(null);
  const [copiedResetLink, setCopiedResetLink] = useState(false);
  const [resetToken, setResetToken] = useState(() => new URLSearchParams(window.location.search).get("token") ?? "");
  const [resetValidationError, setResetValidationError] = useState("");
  const [resetTokenExpiresAt, setResetTokenExpiresAt] = useState("");
  const [resetForm, setResetForm] = useState({
    password: "",
    confirmPassword: "",
  });
  const [resetFormError, setResetFormError] = useState("");
  const [resetFormMessage, setResetFormMessage] = useState("");
  const [isResetValidating, setIsResetValidating] = useState(false);
  const [isResetSubmitting, setIsResetSubmitting] = useState(false);

  const isResetRoute = routePath === RESET_PASSWORD_PATH;
  const isAuthenticated = Boolean(authUser);
  const userRoles = authUser?.roles ?? [];
  const canManageUsers = userRoles.includes("admin");
  const canManagePowerBI = userRoles.includes("admin");
  const canManagePipeline = userRoles.includes("admin");
  const canRunPipeline = isAuthenticated;
  const canSeeUploads = canManageUsers || (authUser?.uploadScope ?? "all") === "all";
  const visibleSettingsSections = settingsSections.filter((section) => {
    if (section.key === "profile") {
      return true;
    }
    if (section.key === "pipeline") {
      return canManagePipeline;
    }
    return canManageUsers;
  });

  useEffect(() => {
    setProfileForm({
      email: authUser?.email ?? "",
      fullName: authUser?.fullName ?? "",
    });
  }, [authUser?.email, authUser?.fullName]);

  useEffect(() => {
    if (!visibleSettingsSections.some((section) => section.key === activeSettingsSection)) {
      setActiveSettingsSection(visibleSettingsSections[0]?.key ?? "profile");
    }
  }, [activeSettingsSection, visibleSettingsSections]);

  async function refreshSession() {
    const response = await fetch("/api/auth/session");
    const data = await response.json();
    setCsrfToken(data.csrfToken ?? "");
    setAuthUser(data.authenticated ? data.user ?? null : null);
    return data;
  }

  async function apiRequest(path, options = {}) {
    const method = options.method ?? "GET";
    const headers = {
      Accept: "application/json",
      ...(options.headers ?? {}),
    };

    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    if (method !== "GET" && method !== "HEAD" && csrfToken) {
      headers["X-CSRF-Token"] = csrfToken;
    }

    const response = await fetch(path, {
      method,
      headers,
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    });

    let data = {};
    try {
      data = await response.json();
    } catch (parseError) {
      data = {};
    }

    if (!response.ok) {
      const requestError = new Error(data.error || `Request failed with status ${response.status}.`);
      requestError.status = response.status;
      if (response.status === 401) {
        setAuthUser(null);
      }
      throw requestError;
    }

    if (data.csrfToken) {
      setCsrfToken(data.csrfToken);
    }

    return data;
  }

  function navigateTo(path) {
    window.history.replaceState({}, "", path);
    setRoutePath(window.location.pathname);
    setResetToken(new URLSearchParams(window.location.search).get("token") ?? "");
  }

  async function loadDashboard(preferredSurveyId = null) {
    setIsLoading(true);
    setError("");

    try {
      const data = await apiRequest("/api/dashboard");
      setDashboard(data);
      const preferredSurveyExists = (data.surveys ?? []).some((survey) => survey.id === preferredSurveyId);
      setSelectedSurveyId(preferredSurveyExists ? preferredSurveyId : null);
    } catch (loadError) {
      setError(loadError.message);
      setDashboard(emptyDashboard);
      setSelectedSurveyId(null);
    } finally {
      setIsLoading(false);
    }
  }

  async function loadEmbeddedPowerBIState() {
    setIsPowerBILoading(true);
    setPowerBIError("");

    try {
      const [selectionsData, embedsData] = await Promise.all([
        apiRequest("/api/powerbi/selections"),
        apiRequest("/api/powerbi/embed-configs"),
      ]);

      const selectedIds = (selectionsData.reports ?? []).map((report) => report.report_id);
      setSavedReportIds(selectedIds);
      setEmbeddedReports(embedsData.reports ?? []);
    } catch (loadError) {
      setSavedReportIds([]);
      setEmbeddedReports([]);
      setPowerBIError(loadError.message);
    } finally {
      setIsPowerBILoading(false);
    }
  }

  async function loadPowerBIAdminState() {
    if (!canManagePowerBI) {
      return;
    }

    try {
      const reportsData = await apiRequest("/api/powerbi/reports");
      setAvailableReports(reportsData.reports ?? []);
      setSelectedPowerBIReports((current) => (current.length > 0 ? current : savedReportIds));
    } catch (loadError) {
      setAvailableReports([]);
      setPowerBIError(loadError.message);
    }
  }

  async function loadPipelineStatus() {
    if (!canManagePipeline) {
      return;
    }

    setIsPipelineStatusLoading(true);
    setPipelineError("");

    try {
      const data = await apiRequest("/api/pipeline/status");
      setPipelineStatus(data);
    } catch (loadError) {
      setPipelineStatus(null);
      setPipelineError(loadError.message);
    } finally {
      setIsPipelineStatusLoading(false);
    }
  }

  async function refreshPipelineView() {
    await loadDashboard(selectedSurveyId);
    if (canManagePipeline) {
      await loadPipelineStatus();
    }
  }

  async function loadUsers() {
    if (!canManageUsers) {
      return;
    }

    setIsUsersLoading(true);
    setUsersError("");

    try {
      const data = await apiRequest("/api/admin/users");
      setUsers(data.users ?? []);
    } catch (loadError) {
      setUsers([]);
      setUsersError(loadError.message);
    } finally {
      setIsUsersLoading(false);
    }
  }

  async function loadRoles() {
    if (!canManageUsers) {
      return;
    }

    setIsRolesLoading(true);
    setRolesError("");

    try {
      const data = await apiRequest("/api/admin/roles");
      setRoles(data.roles ?? []);
    } catch (loadError) {
      setRoles([]);
      setRolesError(loadError.message);
    } finally {
      setIsRolesLoading(false);
    }
  }

  async function validateResetLink(token) {
    setIsResetValidating(true);
    if (!token) {
      setResetValidationError("This reset link is missing a token.");
      setResetTokenExpiresAt("");
      setIsResetValidating(false);
      return;
    }

    try {
      const data = await apiRequest(`/api/auth/reset-password/validate?token=${encodeURIComponent(token)}`);
      setResetValidationError("");
      setResetTokenExpiresAt(data.expiresAt ?? "");
    } catch (validationError) {
      setResetValidationError(validationError.message);
      setResetTokenExpiresAt("");
    } finally {
      setIsResetValidating(false);
    }
  }

  useEffect(() => {
    let isCancelled = false;

    async function bootstrap() {
      try {
        await refreshSession();
      } catch (sessionError) {
        if (!isCancelled) {
          setLoginError(sessionError.message);
        }
      } finally {
        if (!isCancelled) {
          setIsBootstrapping(false);
        }
      }
    }

    bootstrap();

    return () => {
      isCancelled = true;
    };
  }, []);

  useEffect(() => {
    if (isResetRoute) {
      validateResetLink(resetToken);
      return;
    }

    if (isAuthenticated) {
      loadDashboard();
      loadEmbeddedPowerBIState();
    } else {
      setDashboard(emptyDashboard);
      setEmbeddedReports([]);
      setSavedReportIds([]);
      setAvailableReports([]);
      setSelectedPowerBIReports([]);
      setSelectedSurveyId(null);
    }
  }, [isAuthenticated, isResetRoute]);

  useEffect(() => {
    const availableTabs = [
      "surveys",
      "uploads",
      ...embeddedReports.map((report) => `powerbi:${report.reportId}`),
    ];
    if (!availableTabs.includes(activeDashboardTab)) {
      setActiveDashboardTab("surveys");
    }
  }, [embeddedReports, activeDashboardTab]);

  useEffect(() => {
    if (!isAuthenticated || currentView !== "settings") {
      return;
    }

    if (activeSettingsSection === "profile") {
      return;
    }

    if (activeSettingsSection === "users") {
      loadUsers();
      loadRoles();
      return;
    }

    if (activeSettingsSection === "roles") {
      loadRoles();
      loadPowerBIAdminState();
      return;
    }

    if (activeSettingsSection === "powerbi") {
      loadPowerBIAdminState();
      return;
    }

    if (activeSettingsSection === "pipeline") {
      loadPipelineStatus();
    }
  }, [
    isAuthenticated,
    currentView,
    activeSettingsSection,
    canManageUsers,
    canManagePowerBI,
    canManagePipeline,
    savedReportIds,
  ]);

  function handleCredentialsChange(field, value) {
    setCredentials((current) => ({ ...current, [field]: value }));
  }

  async function handleLoginSubmit(event) {
    event.preventDefault();
    setLoginError("");

    const emailValue = credentials.email.trim();
    if (!emailValue) {
      setLoginError("Email is required.");
      return;
    }
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(emailValue)) {
      setLoginError("Enter a valid email address.");
      return;
    }

    try {
      const data = await apiRequest("/api/auth/login", {
        method: "POST",
        body: {
          ...credentials,
          email: emailValue,
        },
      });
      setAuthUser(data.user ?? null);
      setCredentials({ email: "", password: "" });
      setCurrentView("dashboard");
      setRoutePath("/");
      window.history.replaceState({}, "", "/");
    } catch (submitError) {
      setLoginError(toFriendlyLoginError(submitError));
    }
  }

  function handleLoginKeyDown(event) {
    if (event.key !== "Enter" || event.shiftKey || event.metaKey || event.ctrlKey || event.altKey) {
      return;
    }
    event.preventDefault();
    event.currentTarget.requestSubmit();
  }

  async function handleLogout() {
    try {
      const data = await apiRequest("/api/auth/logout", { method: "POST" });
      setAuthUser(data.user ?? null);
      setProfileMessage("");
      setProfileError("");
      setRolesMessage("");
      setRolesError("");
      setPowerBIMessage("");
      setUsersMessage("");
      setIssuedResetLink(null);
    } catch (logoutError) {
      setLoginError(logoutError.message);
      setAuthUser(null);
    }
  }

  async function handleRunPipeline() {
    setIsRunning(true);
    setError("");

    try {
      await apiRequest("/api/pipeline/run", {
        method: "POST",
        body: { extractMode: mode },
      });
    } catch (runError) {
      setError(runError.message);
    } finally {
      await refreshPipelineView();
      setIsRunning(false);
    }
  }

  async function handleRefreshPipelineStatus() {
    setPipelineError("");
    try {
      await refreshPipelineView();
    } catch (refreshError) {
      setPipelineError(refreshError.message);
    }
  }

  async function handlePullPipeline() {
    setIsPullingPipeline(true);
    setPipelineError("");
    setPipelineMessage("");
    setPipelineOutput("");

    try {
      const data = await apiRequest("/api/pipeline/pull", {
        method: "POST",
      });
      setPipelineStatus(data.after ?? data.before ?? null);
      setPipelineOutput(data.output || "No output returned.");
      if (data.status === "blocked") {
        setPipelineError(data.output || "Pipeline pull was blocked.");
      } else {
        setPipelineMessage("Pipeline code updated.");
      }
      await refreshPipelineView();
    } catch (pullError) {
      setPipelineError(pullError.message);
      try {
        await refreshPipelineView();
      } catch (statusError) {
        // The visible error above is the pull failure; status refresh is best-effort.
      }
    } finally {
      setIsPullingPipeline(false);
    }
  }

  async function handleSavePowerBIReports(event) {
    event.preventDefault();
    setIsSavingPowerBI(true);
    setPowerBIError("");
    setPowerBIMessage("");

    try {
      const data = await apiRequest("/api/powerbi/selections", {
        method: "PUT",
        body: { reportIds: selectedPowerBIReports },
      });
      const selectedIds = (data.reports ?? []).map((report) => report.report_id);
      setSavedReportIds(selectedIds);
      setSelectedPowerBIReports(selectedIds);
      setPowerBIMessage("Power BI landing page selection updated.");
      await loadEmbeddedPowerBIState();
      setCurrentView("dashboard");
    } catch (saveError) {
      setPowerBIError(saveError.message);
    } finally {
      setIsSavingPowerBI(false);
    }
  }

  function handleSort(columnKey) {
    setSortConfig((current) => {
      if (current.key === columnKey) {
        return {
          key: columnKey,
          direction: current.direction === "asc" ? "desc" : "asc",
        };
      }

      return {
        key: columnKey,
        direction: columnKey === "last_submission_at" ? "desc" : "asc",
      };
    });
  }

  function handleUploadSort(columnKey) {
    setUploadSortConfig((current) => {
      if (current.key === columnKey) {
        return {
          key: columnKey,
          direction: current.direction === "asc" ? "desc" : "asc",
        };
      }

      return {
        key: columnKey,
        direction: columnKey === "uploaded_at" ? "desc" : "asc",
      };
    });
  }

  function togglePowerBIReport(reportId) {
    setSelectedPowerBIReports((current) => {
      if (current.includes(reportId)) {
        return current.filter((item) => item !== reportId);
      }
      return [...current, reportId];
    });
  }

  function handleNewUserChange(field, value) {
    setNewUserForm((current) => ({ ...current, [field]: value }));
  }

  function resetUserForm() {
    setEditingUserId(null);
    setIsUserFormVisible(false);
    setNewUserForm({
      email: "",
      fullName: "",
      role: roles[0]?.name ?? "viewer",
      password: "",
    });
  }

  function startCreatingUser() {
    setEditingUserId(null);
    setNewUserForm({
      email: "",
      fullName: "",
      role: roles[0]?.name ?? "viewer",
      password: "",
    });
    setUsersError("");
    setUsersMessage("");
    setIsUserFormVisible(true);
  }

  function startEditingUser(user) {
    setEditingUserId(user.id);
    setIsUserFormVisible(true);
    setNewUserForm({
      email: user.email,
      fullName: user.fullName || "",
      role: user.primaryRole || user.roles[0] || roles[0]?.name || "viewer",
      password: "",
    });
    setUsersError("");
    setUsersMessage("");
  }

  function handleProfileChange(field, value) {
    setProfileForm((current) => ({ ...current, [field]: value }));
  }

  function handlePasswordChange(field, value) {
    setPasswordChangeForm((current) => ({ ...current, [field]: value }));
  }

  function resetRoleForm() {
    setIsRoleFormVisible(false);
    setRoleForm({
      id: null,
      name: "",
      description: "",
      projectScope: "all",
      allowedProjectRefs: [],
      reportScope: "all",
      allowedReportIds: [],
      uploadScope: "all",
    });
  }

  function startCreatingRole() {
    setRoleForm({
      id: null,
      name: "",
      description: "",
      projectScope: "all",
      allowedProjectRefs: [],
      reportScope: "all",
      allowedReportIds: [],
      uploadScope: "all",
    });
    setRolesError("");
    setRolesMessage("");
    setIsRoleFormVisible(true);
  }

  function handleRoleFieldChange(field, value) {
    setRoleForm((current) => ({ ...current, [field]: value }));
  }

  function toggleRoleListValue(field, value) {
    setRoleForm((current) => {
      const currentValues = current[field] ?? [];
      return {
        ...current,
        [field]: currentValues.includes(value)
          ? currentValues.filter((item) => item !== value)
          : [...currentValues, value],
      };
    });
  }

  function startEditingRole(role) {
    setIsRoleFormVisible(true);
    setRoleForm({
      id: role.id,
      name: role.name,
      description: role.description || "",
      projectScope: role.projectScope,
      allowedProjectRefs: role.allowedProjectRefs ?? [],
      reportScope: role.reportScope,
      allowedReportIds: role.allowedReportIds ?? [],
      uploadScope: role.uploadScope ?? "all",
    });
    setRolesError("");
    setRolesMessage("");
  }

  async function handleSaveProfile(event) {
    event.preventDefault();
    setIsSavingProfile(true);
    setProfileError("");
    setProfileMessage("");

    const emailValue = profileForm.email.trim().toLowerCase();
    if (!emailValue) {
      setProfileError("Email is required.");
      setIsSavingProfile(false);
      return;
    }
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(emailValue)) {
      setProfileError("Enter a valid email address.");
      setIsSavingProfile(false);
      return;
    }

    try {
      const data = await apiRequest("/api/auth/profile", {
        method: "PATCH",
        body: {
          email: emailValue,
          fullName: profileForm.fullName.trim(),
        },
      });
      setAuthUser(data.user ?? null);
      setProfileMessage("Profile updated.");
    } catch (saveError) {
      setProfileError(saveError.message);
    } finally {
      setIsSavingProfile(false);
    }
  }

  async function handleChangePassword(event) {
    event.preventDefault();
    setIsChangingPassword(true);
    setProfileError("");
    setProfileMessage("");

    if (passwordChangeForm.newPassword !== passwordChangeForm.confirmPassword) {
      setProfileError("New passwords do not match.");
      setIsChangingPassword(false);
      return;
    }

    try {
      const data = await apiRequest("/api/auth/change-password", {
        method: "POST",
        body: {
          currentPassword: passwordChangeForm.currentPassword,
          newPassword: passwordChangeForm.newPassword,
        },
      });
      setAuthUser(data.user ?? null);
      setPasswordChangeForm({
        currentPassword: "",
        newPassword: "",
        confirmPassword: "",
      });
      setProfileMessage(data.message || "Password updated successfully.");
    } catch (changeError) {
      setProfileError(changeError.message);
    } finally {
      setIsChangingPassword(false);
    }
  }

  async function handleSaveRole(event) {
    event.preventDefault();
    setIsSavingRole(true);
    setRolesError("");
    setRolesMessage("");

    try {
      const path = roleForm.id ? `/api/admin/roles/${roleForm.id}` : "/api/admin/roles";
      const method = roleForm.id ? "PATCH" : "POST";
      const data = await apiRequest(path, {
        method,
        body: roleForm,
      });
      const savedRole = data.role;
      setRoles((current) => {
        const nextRoles = roleForm.id
          ? current.map((role) => (role.id === savedRole.id ? savedRole : role))
          : [...current, savedRole];
        return [...nextRoles].sort((left, right) => left.name.localeCompare(right.name));
      });
      setRolesMessage(roleForm.id ? "Role updated." : "Role created.");
      resetRoleForm();
    } catch (saveError) {
      setRolesError(saveError.message);
    } finally {
      setIsSavingRole(false);
    }
  }

  async function handleDeleteRole(role) {
    setDeletingRoleId(role.id);
    setRolesError("");
    setRolesMessage("");

    try {
      await apiRequest(`/api/admin/roles/${role.id}`, {
        method: "DELETE",
      });
      setRoles((current) => current.filter((item) => item.id !== role.id));
      if (roleForm.id === role.id) {
        resetRoleForm();
      }
      setRolesMessage(`Deleted role ${role.name}.`);
    } catch (deleteError) {
      setRolesError(deleteError.message);
    } finally {
      setDeletingRoleId(null);
    }
  }

  async function handleCreateUser(event) {
    event.preventDefault();
    setIsCreatingUser(true);
    setUsersError("");
    setUsersMessage("");

    const emailValue = newUserForm.email.trim().toLowerCase();
    const passwordValue = newUserForm.password;
    if (!emailValue) {
      setUsersError("Email is required.");
      setIsCreatingUser(false);
      return;
    }
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(emailValue)) {
      setUsersError("Enter a valid email address.");
      setIsCreatingUser(false);
      return;
    }
    if (!newUserForm.role) {
      setUsersError("Select a role.");
      setIsCreatingUser(false);
      return;
    }
    if (!editingUserId && passwordValue.length < 8) {
      setUsersError("Temporary password must be at least 8 characters.");
      setIsCreatingUser(false);
      return;
    }

    try {
      const path = editingUserId ? `/api/admin/users/${editingUserId}` : "/api/admin/users";
      const method = editingUserId ? "PATCH" : "POST";
      const body = editingUserId
        ? {
            email: emailValue,
            fullName: newUserForm.fullName,
            role: newUserForm.role,
          }
        : {
            ...newUserForm,
            email: emailValue,
            password: passwordValue,
          };
      const data = await apiRequest(path, {
        method,
        body,
      });
      setUsers((current) => {
        const nextUsers = editingUserId
          ? current.map((user) => (user.id === data.user.id ? data.user : user))
          : [...current, data.user];
        return [...nextUsers].sort((left, right) => left.email.localeCompare(right.email));
      });
      setUsersMessage(editingUserId ? `Updated ${data.user.email}.` : `Created ${data.user.email}.`);
      resetUserForm();
    } catch (createError) {
      setUsersError(createError.message);
    } finally {
      setIsCreatingUser(false);
    }
  }

  async function handleIssueResetLink(user) {
    setIssuingResetUserId(user.id);
    setUsersError("");
    setUsersMessage("");
    setCopiedResetLink(false);

    try {
      const data = await apiRequest(`/api/admin/users/${user.id}/reset-link`, {
        method: "POST",
      });
      setIssuedResetLink({
        userEmail: user.email,
        resetUrl: data.resetUrl,
        expiresAt: data.expiresAt,
      });
      setUsersMessage(`Issued a password reset link for ${user.email}.`);
    } catch (issueError) {
      setUsersError(issueError.message);
      setIssuedResetLink(null);
    } finally {
      setIssuingResetUserId(null);
    }
  }

  async function handleCopyResetLink() {
    if (!issuedResetLink?.resetUrl) {
      return;
    }

    try {
      await navigator.clipboard.writeText(issuedResetLink.resetUrl);
      setCopiedResetLink(true);
      window.setTimeout(() => setCopiedResetLink(false), 1800);
    } catch (copyError) {
      setUsersError("Unable to copy the reset link.");
    }
  }

  async function handleResetPasswordSubmit(event) {
    event.preventDefault();
    setResetFormError("");
    setResetFormMessage("");

    if (resetForm.password !== resetForm.confirmPassword) {
      setResetFormError("Passwords do not match.");
      return;
    }

    setIsResetSubmitting(true);
    try {
      const data = await apiRequest("/api/auth/reset-password", {
        method: "POST",
        body: {
          token: resetToken,
          password: resetForm.password,
        },
      });
      setResetFormMessage(data.message || "Password updated.");
      setResetForm({ password: "", confirmPassword: "" });
      setResetValidationError("");
      setResetTokenExpiresAt("");
      window.setTimeout(() => navigateTo("/"), 1200);
    } catch (submitError) {
      setResetFormError(submitError.message);
    } finally {
      setIsResetSubmitting(false);
    }
  }

  const normalizedFilter = surveyFilter.trim().toLowerCase();
  const filteredSurveys = dashboard.surveys.filter((survey) => {
    if (!normalizedFilter) {
      return true;
    }

    return [
      survey.survey_name,
      survey.country,
      survey.client,
      survey.phase,
      survey.project_ref,
      survey.assessor,
    ]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(normalizedFilter));
  });
  const sortedSurveys = [...filteredSurveys].sort((left, right) => {
    const column = surveyColumns.find((item) => item.key === sortConfig.key) ?? surveyColumns[0];
    const comparison = compareSurveyValues(left[column.key], right[column.key], column.type);
    return sortConfig.direction === "asc" ? comparison : -comparison;
  });
  const normalizedUploadFilter = uploadFilter.trim().toLowerCase();
  const uploadsWithFolders = dashboard.uploads.map((item) => ({
    ...item,
    folder: getSharePointFolder(item),
  }));
  const filteredUploads = uploadsWithFolders.filter((item) => {
    if (!normalizedUploadFilter) {
      return true;
    }

    return [item.file_name, item.folder, item.local_path, item.sharepoint_path, item.status, item.web_url]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(normalizedUploadFilter));
  });
  const sortedUploads = [...filteredUploads].sort((left, right) => {
    const column = uploadColumns.find((item) => item.key === uploadSortConfig.key) ?? uploadColumns[0];
    const leftValue = column.key === "web_url" ? left.web_url || "N/A" : left[column.key];
    const rightValue = column.key === "web_url" ? right.web_url || "N/A" : right[column.key];
    const comparison = compareSurveyValues(leftValue, rightValue, column.type);
    return uploadSortConfig.direction === "asc" ? comparison : -comparison;
  });
  const normalizedUserFilter = userSearch.trim().toLowerCase();
  const filteredUsers = users.filter((user) => {
    if (!normalizedUserFilter) {
      return true;
    }
    return [user.email, user.fullName, user.primaryRole, ...(user.roles ?? [])]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(normalizedUserFilter));
  });
  const normalizedRoleFilter = roleSearch.trim().toLowerCase();
  const filteredRoles = roles.filter((role) => {
    if (!normalizedRoleFilter) {
      return true;
    }
    return [
      role.name,
      role.description,
      role.projectScope,
      role.reportScope,
      role.uploadScope,
      ...(role.allowedProjectRefs ?? []),
      ...(role.allowedReportIds ?? []),
    ]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(normalizedRoleFilter));
  });

  const selectedSurvey = dashboard.surveys.find((survey) => survey.id === selectedSurveyId) ?? null;
  const totalSubmissions = dashboard.surveys.reduce((sum, survey) => sum + survey.submission_count, 0);
  const lastSubmissionAt = dashboard.surveys.reduce((latest, survey) => {
    const candidate = survey.last_submission_at;
    if (!candidate) {
      return latest;
    }
    if (!latest) {
      return candidate;
    }
    return new Date(candidate).getTime() > new Date(latest).getTime() ? candidate : latest;
  }, null);
  const selectedPreview = selectedSurvey?.preview ?? {};
  const dailySubmissionCounts = selectedPreview.daily_submission_counts ?? [];
  const entityDailyCounts = selectedPreview.entity_daily_counts ?? [];
  const enumeratorDailyCounts = selectedPreview.enumerator_daily_counts ?? [];
  const dailySubmissionRows = dailySubmissionCounts.map((item) => {
    const entityTypes = entityDailyCounts
      .filter((entry) => entry.date === item.date)
      .sort(
        (left, right) =>
          Number(right.count ?? 0) - Number(left.count ?? 0) ||
          String(left.entity_type ?? "").localeCompare(String(right.entity_type ?? "")),
      )
      .map((entry) => `${entry.entity_type || "Unknown entity type"} (${entry.count ?? 0})`);
    const enumerators = enumeratorDailyCounts
      .filter((entry) => entry.date === item.date)
      .sort(
        (left, right) =>
          Number(right.count ?? 0) - Number(left.count ?? 0) ||
          String(left.enumerator ?? "").localeCompare(String(right.enumerator ?? "")),
      )
      .map((entry) => `${entry.enumerator || "Unknown enumerator"} (${entry.count ?? 0})`);

    return {
      ...item,
      entityTypes,
      enumerators,
    };
  });
  const uniqueEnumerators = selectedPreview.active_enumerator_count ?? 0;
  const uniqueEntityTypes = selectedPreview.entity_type_count ?? 0;
  const entityTypeTotals = formatList(selectedPreview.entity_type_totals ?? selectedPreview.most_entity_types ?? []);
  const mostTargetGroups = formatList(selectedPreview.most_target_groups ?? []);
  const enabledModules = buildEnabledModules(selectedSurvey);
  const projectOptions = [...new Set(dashboard.surveys.map((survey) => survey.project_ref).filter(Boolean))].sort((left, right) =>
    left.localeCompare(right),
  );
  const activeSettings =
    visibleSettingsSections.find((section) => section.key === activeSettingsSection) ?? visibleSettingsSections[0];
  const dashboardTabs = [
    { key: "surveys", label: "Survey list" },
    ...(canSeeUploads ? [{ key: "uploads", label: "SharePoint uploads" }] : []),
    ...embeddedReports.map((report) => ({
      key: `powerbi:${report.reportId}`,
      label: report.reportName || "Power BI dashboard",
    })),
  ];

  if (isBootstrapping) {
    return (
      <main className="login-shell">
        <section className="login-card">
          <p className="eyebrow">Survey data management</p>
          <h1>
            ALP Metrics <span>Portal</span>
          </h1>
          <p className="hero-text">Loading workspace…</p>
        </section>
      </main>
    );
  }

  if (isResetRoute) {
    return (
      <main className="login-shell">
        <section className="login-card">
          <p className="eyebrow">Password reset</p>
          <h1>Set a new password</h1>
          <p className="hero-text">Use the admin-issued reset link to choose a fresh password.</p>

          {isResetValidating ? <p className="run-note">Checking reset link...</p> : null}
          {resetValidationError && !isResetValidating ? <p className="login-error">{resetValidationError}</p> : null}
          {resetTokenExpiresAt ? <p className="run-note">This link expires on {formatDate(resetTokenExpiresAt)}.</p> : null}
          {resetFormMessage ? <p className="run-note">{resetFormMessage}</p> : null}

          {!resetValidationError && !isResetValidating ? (
            <form className="login-form" onSubmit={handleResetPasswordSubmit} noValidate>
              <label htmlFor="new-password">New password</label>
              <input
                id="new-password"
                type="password"
                value={resetForm.password}
                onChange={(event) => setResetForm((current) => ({ ...current, password: event.target.value }))}
                autoComplete="new-password"
              />

              <label htmlFor="confirm-password">Confirm password</label>
              <input
                id="confirm-password"
                type="password"
                value={resetForm.confirmPassword}
                onChange={(event) => setResetForm((current) => ({ ...current, confirmPassword: event.target.value }))}
                autoComplete="new-password"
              />

              <button type="submit" disabled={isResetSubmitting}>
                {isResetSubmitting ? "Updating password..." : "Update password"}
              </button>
              {resetFormError ? <p className="login-error">{resetFormError}</p> : null}
            </form>
          ) : null}

          <button type="button" className="secondary-button login-secondary-button" onClick={() => navigateTo("/")}>
            Back to sign in
          </button>
        </section>
      </main>
    );
  }

  if (!isAuthenticated) {
    return (
      <main className="login-shell">
        <section className="login-card">
          <p className="eyebrow">Survey data management</p>
          <h1>
            ALP Metrics <span>Portal</span>
          </h1>

          <form className="login-form" onSubmit={handleLoginSubmit} onKeyDown={handleLoginKeyDown} noValidate>
            <label htmlFor="email">Email</label>
            <input
              id="email"
              type="text"
              value={credentials.email}
              onChange={(event) => handleCredentialsChange("email", event.target.value)}
              autoComplete="username"
              inputMode="email"
            />

            <label htmlFor="password">Password</label>
            <input
              id="password"
              type="password"
              value={credentials.password}
              onChange={(event) => handleCredentialsChange("password", event.target.value)}
              autoComplete="current-password"
            />

            <button type="submit">Sign in</button>
            {loginError ? <p className="login-error">{loginError}</p> : null}
          </form>

          <p className="run-note login-forgot-note">Forgot your password? Ask an admin to issue a reset link.</p>
        </section>
      </main>
    );
  }

  if (currentView === "settings") {
    return (
      <main className="page-shell">
        <section className="hero-card">
          <div className="hero-copy">
            <p className="eyebrow">Workspace configuration</p>
            <h1>Settings</h1>
            <p className="hero-text">Manage dashboard visibility, user access, and future workspace controls.</p>
          </div>

          <div className="run-panel">
            <button type="button" onClick={() => setCurrentView("dashboard")}>
              Back to dashboard
            </button>
            <button type="button" className="secondary-button" onClick={handleLogout}>
              Log out
            </button>
            <p className="run-note">
              Active section: <strong>{activeSettings.label}</strong>
            </p>
          </div>
        </section>

        {(profileError || profileMessage) && activeSettingsSection === "profile" ? (
          <section className={`alert-card${profileMessage && !profileError ? " alert-card-success" : ""}`}>
            {profileError || profileMessage}
          </section>
        ) : null}
        {(powerBIError || powerBIMessage) && activeSettingsSection === "powerbi" ? (
          <section className={`alert-card${powerBIMessage && !powerBIError ? " alert-card-success" : ""}`}>
            {powerBIError || powerBIMessage}
          </section>
        ) : null}
        {(usersError || usersMessage) && activeSettingsSection === "users" ? (
          <section className={`alert-card${usersMessage && !usersError ? " alert-card-success" : ""}`}>
            {usersError || usersMessage}
          </section>
        ) : null}
        {(rolesError || rolesMessage) && activeSettingsSection === "roles" ? (
          <section className={`alert-card${rolesMessage && !rolesError ? " alert-card-success" : ""}`}>
            {rolesError || rolesMessage}
          </section>
        ) : null}
        {(pipelineError || pipelineMessage) && activeSettingsSection === "pipeline" ? (
          <section className={`alert-card${pipelineMessage && !pipelineError ? " alert-card-success" : ""}`}>
            {pipelineError || pipelineMessage}
          </section>
        ) : null}

        <section className="settings-layout">
          <aside className="settings-sidebar detail-card">
            <div className="section-heading">
              <h2>Settings sections</h2>
              <p>Select an area to configure.</p>
            </div>

            <div className="settings-nav">
              {visibleSettingsSections.map((section) => (
                <button
                  key={section.key}
                  type="button"
                  className={`settings-nav-button${section.key === activeSettingsSection ? " settings-nav-button-active" : ""}`}
                  onClick={() => setActiveSettingsSection(section.key)}
                >
                  <span>{section.label}</span>
                  <small>{section.description}</small>
                </button>
              ))}
            </div>
          </aside>

          <section className="settings-content detail-card">
            <div className="section-heading">
              <h2>{activeSettings.label}</h2>
              <p>{activeSettings.description}</p>
            </div>

            {activeSettingsSection === "profile" ? (
              <div className="settings-stack">
                <form className="powerbi-settings-form" onSubmit={handleSaveProfile} noValidate>
                  <div className="settings-summary">
                    <div className="stat-card compact-stat-card">
                      <span>Current role</span>
                      <strong>{userRoles.join(", ")}</strong>
                    </div>
                    <div className="stat-card compact-stat-card">
                      <span>Account email</span>
                      <strong>{authUser.email}</strong>
                    </div>
                  </div>

                  <div className="filter-row">
                    <label className="filter-label" htmlFor="profile-full-name">
                      Full name
                    </label>
                    <input
                      id="profile-full-name"
                      type="text"
                      value={profileForm.fullName}
                      onChange={(event) => handleProfileChange("fullName", event.target.value)}
                      autoComplete="name"
                    />
                  </div>

                  <div className="filter-row">
                    <label className="filter-label" htmlFor="profile-email">
                      Email
                    </label>
                    <input
                      id="profile-email"
                      type="text"
                      value={profileForm.email}
                      onChange={(event) => handleProfileChange("email", event.target.value)}
                      autoComplete="email"
                      inputMode="email"
                    />
                  </div>

                  <div className="settings-actions">
                    <button type="submit" disabled={isSavingProfile}>
                      {isSavingProfile ? "Saving profile..." : "Save profile"}
                    </button>
                  </div>
                </form>

                <form className="powerbi-settings-form" onSubmit={handleChangePassword} noValidate>
                  <div className="section-heading">
                    <h2>Change password</h2>
                    <p>Confirm your current password before setting a new one.</p>
                  </div>

                  <div className="filter-row">
                    <label className="filter-label" htmlFor="current-password">
                      Current password
                    </label>
                    <input
                      id="current-password"
                      type="password"
                      value={passwordChangeForm.currentPassword}
                      onChange={(event) => handlePasswordChange("currentPassword", event.target.value)}
                      autoComplete="current-password"
                    />
                  </div>

                  <div className="filter-row">
                    <label className="filter-label" htmlFor="new-account-password">
                      New password
                    </label>
                    <input
                      id="new-account-password"
                      type="password"
                      value={passwordChangeForm.newPassword}
                      onChange={(event) => handlePasswordChange("newPassword", event.target.value)}
                      autoComplete="new-password"
                    />
                  </div>

                  <div className="filter-row">
                    <label className="filter-label" htmlFor="confirm-account-password">
                      Confirm new password
                    </label>
                    <input
                      id="confirm-account-password"
                      type="password"
                      value={passwordChangeForm.confirmPassword}
                      onChange={(event) => handlePasswordChange("confirmPassword", event.target.value)}
                      autoComplete="new-password"
                    />
                  </div>

                  <div className="settings-actions">
                    <button type="submit" disabled={isChangingPassword}>
                      {isChangingPassword ? "Updating password..." : "Update password"}
                    </button>
                  </div>
                </form>
              </div>
            ) : null}

            {activeSettingsSection === "users" ? (
              canManageUsers ? (
                <div className="settings-stack">
                  <div className="settings-summary">
                    <div className="stat-card compact-stat-card">
                      <span>Total users</span>
                      <strong>{users.length}</strong>
                    </div>
                      <div className="stat-card compact-stat-card">
                        <span>Reset link lifetime</span>
                      <strong>{dashboard.settings?.resetLinkHours ?? 48} hours</strong>
                      </div>
                  </div>

                  {!isUserFormVisible ? (
                    <div className="settings-actions">
                      <button type="button" onClick={startCreatingUser}>
                        Create user
                      </button>
                    </div>
                  ) : null}

                  {isUserFormVisible ? (
                    <form className="powerbi-settings-form" onSubmit={handleCreateUser} noValidate>

                    <div className="filter-row">
                      <label className="filter-label" htmlFor="new-user-email">
                        Email
                      </label>
                      <input
                        id="new-user-email"
                        type="email"
                        value={newUserForm.email}
                        onChange={(event) => handleNewUserChange("email", event.target.value)}
                      />
                    </div>

                    <div className="filter-row">
                      <label className="filter-label" htmlFor="new-user-name">
                        Full name
                      </label>
                      <input
                        id="new-user-name"
                        type="text"
                        value={newUserForm.fullName}
                        onChange={(event) => handleNewUserChange("fullName", event.target.value)}
                      />
                    </div>

                    <div className="filter-row">
                      <label className="filter-label" htmlFor="new-user-role">
                        Role
                      </label>
                      <select
                        id="new-user-role"
                        value={newUserForm.role}
                        onChange={(event) => handleNewUserChange("role", event.target.value)}
                        disabled={roles.length === 0}
                      >
                        {roles.map((role) => (
                          <option key={role.id} value={role.name}>
                            {role.name}
                          </option>
                        ))}
                      </select>
                    </div>

                    <div className="filter-row">
                      <label className="filter-label" htmlFor="new-user-password">
                        Temporary password
                      </label>
                      <input
                        id="new-user-password"
                        type="password"
                        value={newUserForm.password}
                        onChange={(event) => handleNewUserChange("password", event.target.value)}
                        autoComplete="new-password"
                      />
                    </div>

                    <div className="settings-actions">
                      <button type="submit" disabled={isCreatingUser || roles.length === 0}>
                        {isCreatingUser ? (editingUserId ? "Saving user..." : "Creating user...") : editingUserId ? "Save user" : "Create user"}
                      </button>
                      {editingUserId ? (
                        <button type="button" className="secondary-button" onClick={resetUserForm}>
                          Cancel edit
                        </button>
                      ) : (
                        <button type="button" className="secondary-button" onClick={resetUserForm}>
                          Cancel
                        </button>
                      )}
                    </div>
                    </form>
                  ) : null}

                  {issuedResetLink ? (
                    <div className="detail-section-block">
                      <div className="detail-section-heading">
                        <h3>Latest reset link</h3>
                        <p>Share this link directly with the user.</p>
                      </div>
                      <div className="preview-list">
                        <div className="preview-item">
                          <div className="preview-heading">
                            <strong>{issuedResetLink.userEmail}</strong>
                            <span>Expires {formatDate(issuedResetLink.expiresAt)}</span>
                          </div>
                          <div className="preview-metadata">
                            <span>{issuedResetLink.resetUrl}</span>
                            <button
                              type="button"
                              className="secondary-button secondary-button-compact"
                              onClick={handleCopyResetLink}
                            >
                              {copiedResetLink ? "Copied" : "Copy"}
                            </button>
                            <a className="sharepoint-link-button" href={issuedResetLink.resetUrl} target="_blank" rel="noreferrer">
                              Open reset link
                            </a>
                          </div>
                        </div>
                      </div>
                    </div>
                  ) : null}

                  {isUsersLoading ? <div className="table-empty">Loading users...</div> : null}

                  {!isUsersLoading && users.length > 0 ? (
                    <div className="filter-row">
                      <label className="filter-label" htmlFor="user-filter">
                        Filter users
                      </label>
                      <input
                        id="user-filter"
                        type="search"
                        value={userSearch}
                        onChange={(event) => setUserSearch(event.target.value)}
                        placeholder="Email, name, or role"
                      />
                    </div>
                  ) : null}

                  {!isUsersLoading && filteredUsers.length > 0 ? (
                    <div className="table-wrap">
                      <table>
                        <thead>
                          <tr>
                            <th>Email</th>
                            <th>Name</th>
                            <th>Role</th>
                            <th>Action</th>
                          </tr>
                        </thead>
                        <tbody>
                          {filteredUsers.map((user) => (
                            <tr key={user.id}>
                              <td data-label="Email">{user.email}</td>
                              <td data-label="Name">{user.fullName || "N/A"}</td>
                              <td data-label="Role">{user.roles.join(", ")}</td>
                              <td data-label="Action">
                                <div className="settings-actions">
                                  <button
                                    type="button"
                                    className="secondary-button secondary-button-compact"
                                    onClick={() => startEditingUser(user)}
                                  >
                                    Edit user
                                  </button>
                                  <button
                                    type="button"
                                    className="secondary-button secondary-button-compact"
                                    onClick={() => handleIssueResetLink(user)}
                                    disabled={issuingResetUserId === user.id}
                                  >
                                    {issuingResetUserId === user.id ? "Issuing..." : "Issue reset link"}
                                  </button>
                                </div>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : null}
                  {!isUsersLoading && users.length > 0 && filteredUsers.length === 0 ? (
                    <div className="table-empty">No users match this filter.</div>
                  ) : null}
                </div>
              ) : (
                <div className="settings-placeholder">
                  <p>User administration is limited to admin accounts.</p>
                </div>
              )
            ) : null}

            {activeSettingsSection === "pipeline" ? (
              canManagePipeline ? (
                <div className="settings-stack">
                  <div className="settings-actions">
                    <button type="button" onClick={handlePullPipeline} disabled={isPullingPipeline || isRunning}>
                      {isPullingPipeline ? "Pulling pipeline..." : "Pull latest pipeline code"}
                    </button>
                    <button type="button" className="secondary-button" onClick={handleRefreshPipelineStatus} disabled={isPipelineStatusLoading}>
                      {isPipelineStatusLoading ? "Refreshing..." : "Refresh status"}
                    </button>
                    <button type="button" className="secondary-button" onClick={handleRunPipeline} disabled={isRunning}>
                      {isRunning ? "Running pipeline..." : "Run pipeline now"}
                    </button>
                  </div>

                  {pipelineOutput ? (
                    <div className="detail-section-block">
                      <div className="detail-section-heading">
                        <h3>Latest pull output</h3>
                        <p>Output from the most recent pipeline code update.</p>
                      </div>
                      <pre className="pipeline-log">{pipelineOutput}</pre>
                    </div>
                  ) : null}

                  {dashboard.latest_run ? (
                    <div className="detail-section-block">
                      <div className="settings-summary">
                        <div className="stat-card compact-stat-card">
                          <span>Run commit</span>
                          <strong>{dashboard.latest_run.pipeline_commit_after?.slice(0, 7) || "N/A"}</strong>
                          <p className="commit-card-meta">
                            Branch: {dashboard.latest_run.pipeline_branch || pipelineStatus?.branch || "N/A"}
                          </p>
                          <p className="commit-card-meta">
                            {dashboard.latest_run.pipeline_commit_subject || "Commit message unavailable."}
                          </p>
                          <p className="commit-card-meta">
                            {formatDate(dashboard.latest_run.pipeline_commit_at)}
                            {dashboard.latest_run.pipeline_commit_author
                              ? ` by ${dashboard.latest_run.pipeline_commit_author}`
                              : ""}
                          </p>
                        </div>
                        <div className="stat-card compact-stat-card">
                          <span>Last run</span>
                          <strong>{formatDate(dashboard.latest_run.completed_at)}</strong>
                          <p className="commit-card-meta">
                            Triggered by:{" "}
                            {dashboard.latest_run.triggered_by_name || dashboard.latest_run.triggered_by_email || "N/A"}
                          </p>
                        </div>
                      </div>
                      {dashboard.latest_run.run_log ? (
                        <pre className="pipeline-log pipeline-log-spaced">{dashboard.latest_run.run_log}</pre>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              ) : (
                <div className="settings-placeholder">
                  <p>Pipeline management is limited to admin accounts.</p>
                </div>
              )
            ) : null}

            {activeSettingsSection === "roles" ? (
              canManageUsers ? (
                <div className="settings-stack">
                  <div className="settings-summary">
                    <div className="stat-card compact-stat-card">
                      <span>Available roles</span>
                      <strong>{roles.length}</strong>
                    </div>
                    <div className="stat-card compact-stat-card">
                      <span>Projects in data</span>
                      <strong>{projectOptions.length}</strong>
                    </div>
                  </div>

                  {!isRoleFormVisible ? (
                    <div className="settings-actions">
                      <button type="button" onClick={startCreatingRole}>
                        Create role
                      </button>
                    </div>
                  ) : null}

                  {isRoleFormVisible ? (
                    <form className="powerbi-settings-form" onSubmit={handleSaveRole} noValidate>

                    <div className="filter-row">
                      <label className="filter-label" htmlFor="role-name">
                        Role name
                      </label>
                      <input
                        id="role-name"
                        type="text"
                        value={roleForm.name}
                        onChange={(event) => handleRoleFieldChange("name", event.target.value)}
                      />
                    </div>

                    <div className="filter-row">
                      <label className="filter-label" htmlFor="role-description">
                        Description
                      </label>
                      <input
                        id="role-description"
                        type="text"
                        value={roleForm.description}
                        onChange={(event) => handleRoleFieldChange("description", event.target.value)}
                      />
                    </div>

                    <div className="filter-row">
                      <label className="filter-label" htmlFor="role-project-scope">
                        Project access
                      </label>
                      <select
                        id="role-project-scope"
                        value={roleForm.projectScope}
                        onChange={(event) => handleRoleFieldChange("projectScope", event.target.value)}
                      >
                        <option value="all">All projects</option>
                        <option value="restricted">Restricted projects</option>
                      </select>
                    </div>

                    {roleForm.projectScope === "restricted" ? (
                      <div className="report-picker-list">
                        {projectOptions.length === 0 ? <div className="table-empty">No projects are available yet.</div> : null}
                        {projectOptions.map((projectRef) => {
                          const isChecked = roleForm.allowedProjectRefs.includes(projectRef);
                          return (
                            <label
                              key={projectRef}
                              className={`report-picker-item${isChecked ? " report-picker-item-active" : ""}`}
                            >
                              <input
                                type="checkbox"
                                checked={isChecked}
                                onChange={() => toggleRoleListValue("allowedProjectRefs", projectRef)}
                              />
                              <div>
                                <strong>{projectRef}</strong>
                              </div>
                            </label>
                          );
                        })}
                      </div>
                    ) : null}

                    <div className="filter-row">
                      <label className="filter-label" htmlFor="role-report-scope">
                        Power BI access
                      </label>
                      <select
                        id="role-report-scope"
                        value={roleForm.reportScope}
                        onChange={(event) => handleRoleFieldChange("reportScope", event.target.value)}
                      >
                        <option value="all">All dashboards</option>
                        <option value="restricted">Restricted dashboards</option>
                      </select>
                    </div>

                    {roleForm.reportScope === "restricted" ? (
                      <div className="report-picker-list">
                        {availableReports.length === 0 ? <div className="table-empty">No dashboards are available yet.</div> : null}
                        {availableReports.map((report) => {
                          const isChecked = roleForm.allowedReportIds.includes(report.id);
                          return (
                            <label
                              key={report.id}
                              className={`report-picker-item${isChecked ? " report-picker-item-active" : ""}`}
                            >
                              <input
                                type="checkbox"
                                checked={isChecked}
                                onChange={() => toggleRoleListValue("allowedReportIds", report.id)}
                              />
                              <div>
                                <strong>{report.name || "Untitled report"}</strong>
                                <span>{report.id}</span>
                              </div>
                            </label>
                          );
                        })}
                      </div>
                    ) : null}

                    <div className="filter-row">
                      <label className="filter-label" htmlFor="role-upload-scope">
                        SharePoint uploads access
                      </label>
                      <select
                        id="role-upload-scope"
                        value={roleForm.uploadScope}
                        onChange={(event) => handleRoleFieldChange("uploadScope", event.target.value)}
                      >
                        <option value="all">Can see uploads</option>
                        <option value="none">Hide uploads</option>
                      </select>
                    </div>

                    <div className="settings-actions">
                      <button type="submit" disabled={isSavingRole}>
                        {isSavingRole ? "Saving role..." : roleForm.id ? "Update role" : "Create role"}
                      </button>
                      <button type="button" className="secondary-button" onClick={resetRoleForm}>
                        {roleForm.id ? "Cancel edit" : "Cancel"}
                      </button>
                    </div>
                    </form>
                  ) : null}

                  {isRolesLoading ? <div className="table-empty">Loading roles...</div> : null}

                  {!isRolesLoading && roles.length > 0 ? (
                    <div className="filter-row">
                      <label className="filter-label" htmlFor="role-filter">
                        Filter roles
                      </label>
                      <input
                        id="role-filter"
                        type="search"
                        value={roleSearch}
                        onChange={(event) => setRoleSearch(event.target.value)}
                        placeholder="Role, description, or access"
                      />
                    </div>
                  ) : null}

                  {!isRolesLoading && filteredRoles.length > 0 ? (
                    <div className="table-wrap">
                      <table>
                        <thead>
                          <tr>
                            <th>Role</th>
                            <th>Projects</th>
                            <th>Dashboards</th>
                            <th>Uploads</th>
                            <th>Users</th>
                            <th>Action</th>
                          </tr>
                        </thead>
                        <tbody>
                          {filteredRoles.map((role) => (
                            <tr key={role.id}>
                              <td data-label="Role">
                                <strong>{role.name}</strong>
                              </td>
                              <td data-label="Projects">
                                {role.projectScope === "all" ? "All" : `${role.allowedProjectRefs.length} selected`}
                              </td>
                              <td data-label="Dashboards">
                                {role.reportScope === "all" ? "All" : `${role.allowedReportIds.length} selected`}
                              </td>
                              <td data-label="Uploads">{role.uploadScope === "all" ? "Visible" : "Hidden"}</td>
                              <td data-label="Users">{role.userCount}</td>
                              <td data-label="Action">
                                <div className="settings-actions">
                                  <button
                                    type="button"
                                    className="secondary-button secondary-button-compact"
                                    onClick={() => startEditingRole(role)}
                                    disabled={role.isSystem}
                                  >
                                    Edit
                                  </button>
                                  <button
                                    type="button"
                                    className="secondary-button secondary-button-compact"
                                    onClick={() => handleDeleteRole(role)}
                                    disabled={role.isSystem || deletingRoleId === role.id}
                                  >
                                    {deletingRoleId === role.id ? "Deleting..." : "Delete"}
                                  </button>
                                </div>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : null}
                  {!isRolesLoading && roles.length > 0 && filteredRoles.length === 0 ? (
                    <div className="table-empty">No roles match this filter.</div>
                  ) : null}
                </div>
              ) : (
                <div className="settings-placeholder">
                  <p>Role administration is limited to admin accounts.</p>
                </div>
              )
            ) : null}

            {activeSettingsSection === "powerbi" ? (
              canManagePowerBI ? (
                <form className="powerbi-settings-form" onSubmit={handleSavePowerBIReports}>
                  <div className="settings-summary">
                    <div className="stat-card compact-stat-card">
                      <span>Workspace reports</span>
                      <strong>{availableReports.length}</strong>
                    </div>
                    <div className="stat-card compact-stat-card">
                      <span>Shown on landing page</span>
                      <strong>{savedReportIds.length}</strong>
                    </div>
                  </div>

                  {isPowerBILoading ? <div className="table-empty">Loading Power BI reports...</div> : null}

                  {!isPowerBILoading && availableReports.length === 0 ? (
                    <div className="table-empty">No Power BI reports are visible in the configured workspace yet.</div>
                  ) : null}

                  {!isPowerBILoading && availableReports.length > 0 ? (
                    <div className="report-picker-list">
                      {availableReports.map((report) => {
                        const isChecked = selectedPowerBIReports.includes(report.id);
                        return (
                          <label key={report.id} className={`report-picker-item${isChecked ? " report-picker-item-active" : ""}`}>
                            <input
                              type="checkbox"
                              checked={isChecked}
                              onChange={() => togglePowerBIReport(report.id)}
                            />
                            <div>
                              <strong>{report.name || "Untitled report"}</strong>
                              <span>{report.id}</span>
                              <small>{report.datasetId || "No dataset ID"}</small>
                              {report.isEffectiveIdentityRequired ? (
                                <small>Requires effective identity (RLS) for embedding.</small>
                              ) : null}
                            </div>
                          </label>
                        );
                      })}
                    </div>
                  ) : null}

                  <div className="settings-actions">
                    <button type="submit" disabled={isSavingPowerBI}>
                      {isSavingPowerBI ? "Saving dashboard selection..." : "Save landing page dashboards"}
                    </button>
                    <button
                      type="button"
                      className="secondary-button"
                      onClick={() => setSelectedPowerBIReports(savedReportIds)}
                    >
                      Reset to saved selection
                    </button>
                  </div>
                </form>
              ) : (
                <div className="settings-placeholder">
                  <p>Power BI dashboard management is limited to admin accounts.</p>
                </div>
              )
            ) : null}

            {!["profile", "users", "roles", "pipeline", "powerbi"].includes(activeSettingsSection) ? (
              <div className="settings-placeholder">
                <p>{activeSettings.description}</p>
                <p>This section is a placeholder for the next phase.</p>
              </div>
            ) : null}
          </section>
        </section>

        <BrandingFooter />
      </main>
    );
  }

  return (
    <main className="page-shell">
      <section className="hero-card">
        <div className="hero-copy">
          <p className="eyebrow">Survey data management</p>
          <h1>
            ALP Metrics <span>Portal</span>
          </h1>
          <p className="hero-text">
            Run the pipeline, track project activity, and review reporting dashboards all in one place.
          </p>

          <div className="hero-copy-meta">
            <p className="run-meta run-meta-user">
              <span>Signed in as: </span>
              <strong>{authUser.fullName || authUser.email}</strong>
            </p>
            <p className="run-meta">Role: {userRoles.join(", ")}</p>
          </div>

          <div className="hero-copy-actions">
            <button type="button" className="hero-action-button hero-action-button-muted" onClick={() => setCurrentView("settings")}>
              Open settings
            </button>
            <button type="button" className="hero-action-button hero-action-button-muted" onClick={handleLogout}>
              Log out
            </button>
          </div>
        </div>

        <div className="run-panel">
          <label htmlFor="extract-mode">Data source</label>
          <select id="extract-mode" value={mode} onChange={(event) => setMode(event.target.value)} disabled={!canRunPipeline}>
            <option value="surveycto">SurveyCTO</option>
            <option value="csv">Local CSV</option>
          </select>
          <button type="button" onClick={handleRunPipeline} disabled={isRunning || !canRunPipeline}>
            {isRunning ? "Updating data..." : "Update data"}
          </button>
          <p className="run-meta">
            Last updated by: {dashboard.latest_run?.triggered_by_name || dashboard.latest_run?.triggered_by_email || "N/A"}
          </p>
          <p className="run-meta">Last updated at: {formatDate(dashboard.latest_run?.completed_at)}</p>
        </div>
      </section>

      {error ? <section className="alert-card">{error}</section> : null}
      {powerBIError && !embeddedReports.length ? <section className="alert-card">{powerBIError}</section> : null}
      {powerBIMessage ? <section className="alert-card alert-card-success">{powerBIMessage}</section> : null}

      <section className="stats-grid">
        <article className="stat-card">
          <span>Projects</span>
          <strong>{dashboard.surveys.length}</strong>
        </article>
        <article className="stat-card">
          <span>Assessments completed</span>
          <strong>{totalSubmissions}</strong>
        </article>
        <article className="stat-card">
          <span>Last submission</span>
          <strong>{formatDate(lastSubmissionAt)}</strong>
        </article>
        <article className="stat-card">
          <span>Reporting dashboards</span>
          <strong>{embeddedReports.length}</strong>
        </article>
      </section>

      <section className="tab-shell">
        <div className="tab-row">
          {dashboardTabs.map((tab) => (
            <button
              key={tab.key}
              type="button"
              className={`tab-button${activeDashboardTab === tab.key ? " tab-button-active" : ""}`}
              onClick={() => setActiveDashboardTab(tab.key)}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {activeDashboardTab === "surveys" ? (
          <section className="single-panel-grid">
            <article className="detail-card survey-split-panel">
              <div className={`survey-split-grid${selectedSurvey ? " survey-split-grid-active" : " survey-split-grid-list-only"}`}>
                {selectedSurvey ? (
                  <section
                    key={selectedSurvey.id}
                    className="survey-split-column survey-split-column-preview survey-split-column-preview-enter"
                  >
                    <div className="section-heading section-heading-inline section-heading-inline-top">
                      <div>
                        <p className="eyebrow">Selected survey</p>
                        <h2>{selectedSurvey.survey_name}</h2>
                        <p>Details and recent activity for the survey selected in the list.</p>
                      </div>
                      <button
                        type="button"
                        className="secondary-button secondary-button-compact"
                        onClick={() => setSelectedSurveyId(null)}
                      >
                        Hide preview
                      </button>
                    </div>

                    <div className="detail-grid detail-grid-primary detail-grid-compact">
                      <div>
                        <span>Total submissions</span>
                        <strong>{selectedSurvey.submission_count ?? 0}</strong>
                      </div>
                      <div>
                        <span>Most recent activity</span>
                        <strong>{formatDate(selectedSurvey.last_submission_at)}</strong>
                      </div>
                      <div>
                        <span>Active enumerators</span>
                        <strong>{uniqueEnumerators || "N/A"}</strong>
                      </div>
                      <div>
                        <span>Entity types covered</span>
                        <strong>{uniqueEntityTypes || "N/A"}</strong>
                      </div>
                    </div>

                    <div className="detail-section-block">
                      <div className="detail-section-heading">
                        <h3>Project details</h3>
                        <p>Context linked to the currently selected survey.</p>
                      </div>

                      <div className="detail-grid detail-grid-secondary detail-grid-compact">
                        <div>
                          <span>Project ref</span>
                          <strong>{selectedSurvey.project_ref || "N/A"}</strong>
                        </div>
                        <div>
                          <span>Assessor</span>
                          <strong>{selectedSurvey.assessor || "N/A"}</strong>
                        </div>
                        <div>
                          <span>Client</span>
                          <strong>{selectedSurvey.client || "N/A"}</strong>
                        </div>
                        <div>
                          <span>Country</span>
                          <strong>{selectedSurvey.country || "N/A"}</strong>
                        </div>
                        <div>
                          <span>Phase</span>
                          <strong>{selectedSurvey.phase || "N/A"}</strong>
                        </div>
                        <div>
                          <span>First submission</span>
                          <strong>{formatDate(selectedSurvey.first_submission_at)}</strong>
                        </div>
                      </div>
                    </div>

                    {enabledModules.length > 0 ? (
                      <div className="module-strip">
                        <span className="module-label">Add-ons</span>
                        <div className="module-chips">
                          {enabledModules.map((moduleName) => (
                            <span key={moduleName} className="module-chip">
                              {moduleName}
                            </span>
                          ))}
                        </div>
                      </div>
                    ) : null}

                    <div className="insight-stack insight-stack-two">
                      <div>
                        <div className="insight-card">
                          <span>Total entity types</span>
                          <strong>{entityTypeTotals}</strong>
                        </div>
                      </div>
                      <div>
                        <div className="insight-card">
                          <span>Most target groups</span>
                          <strong>{mostTargetGroups}</strong>
                        </div>
                      </div>
                    </div>

                    <div className="detail-section-block">
                      <div className="detail-section-heading">
                        <h3>Records per day</h3>
                        <p>Daily entity type and enumerator contributions for recent active days.</p>
                      </div>

                      {dailySubmissionRows.length > 0 ? (
                        <div className="table-wrap aggregate-table-wrap">
                          <table className="aggregate-table">
                            <thead>
                              <tr>
                                <th>Date</th>
                                <th>Entity types</th>
                                <th>Enumerators</th>
                              </tr>
                            </thead>
                            <tbody>
                              {dailySubmissionRows.map((item) => (
                                <tr key={item.date}>
                                  <td data-label="Date">{formatDateOnly(item.date)}</td>
                                  <td data-label="Entity types">
                                    {item.entityTypes.length > 0 ? item.entityTypes.join(", ") : "N/A"}
                                  </td>
                                  <td data-label="Enumerators">
                                    {item.enumerators.length > 0 ? item.enumerators.join(", ") : "N/A"}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      ) : (
                        <div className="table-empty">No daily counts available yet.</div>
                      )}
                    </div>
                  </section>
                ) : null}

                <section
                  className={`survey-split-column survey-split-column-list${selectedSurvey ? " survey-split-column-list-active" : ""}`}
                >
                  <div className="section-heading">
                    <h2>Survey list</h2>
                    {selectedSurvey ? (
                      <p>
                        <span className="inline-instruction">Click another project row</span> to select it and see a quick
                        preview.
                      </p>
                    ) : (
                      <p>
                        Browse the latest project activity. <span className="inline-instruction">Click a project row</span> to
                        select it and see a quick preview.
                      </p>
                    )}
                  </div>

                  <div className="filter-row">
                    <label className="filter-label" htmlFor="survey-filter">
                      Quick filter
                    </label>
                    <input
                      id="survey-filter"
                      type="text"
                      value={surveyFilter}
                      onChange={(event) => setSurveyFilter(event.target.value)}
                    />
                  </div>

                  {isLoading ? (
                    <div className="table-empty">Loading surveys...</div>
                  ) : dashboard.surveys.length === 0 ? (
                    <div className="table-empty">Run the pipeline to populate the survey list.</div>
                  ) : filteredSurveys.length === 0 ? (
                    <div className="table-empty">No surveys match the current filter.</div>
                  ) : (
                    <div className="table-wrap">
                      <table>
                        <thead>
                          <tr>
                            {surveyColumns.map((column) => {
                              const isActive = sortConfig.key === column.key;
                              const sortIndicator = isActive ? (sortConfig.direction === "asc" ? "↑" : "↓") : "↕";

                              return (
                                <th key={column.key}>
                                  <button
                                    type="button"
                                    className={`sort-button${isActive ? " sort-button-active" : ""}`}
                                    onClick={() => handleSort(column.key)}
                                  >
                                    <span>{column.label}</span>
                                    <span className="sort-indicator">{sortIndicator}</span>
                                  </button>
                                </th>
                              );
                            })}
                          </tr>
                        </thead>
                        <tbody>
                          {sortedSurveys.map((survey) => (
                            <tr
                              key={survey.id}
                              className={`survey-list-row${survey.id === selectedSurveyId ? " selected-row" : ""}`}
                              onClick={() => setSelectedSurveyId(survey.id)}
                              title="Click to see a quick preview"
                            >
                              <td data-label="Survey">{survey.survey_name}</td>
                              <td data-label="Project">{survey.project_ref || "N/A"}</td>
                              <td data-label="Country">{survey.country || "N/A"}</td>
                              <td data-label="Client">{survey.client || "N/A"}</td>
                              <td data-label="Phase">{survey.phase || "N/A"}</td>
                              <td data-label="Submissions">{survey.submission_count}</td>
                              <td data-label="Latest">{formatDate(survey.last_submission_at)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </section>
              </div>
            </article>
          </section>
        ) : null}

        {activeDashboardTab === "uploads" ? (
          <section className="single-panel-grid">
            <article className="detail-card survey-split-panel">
              <section className="survey-split-column survey-split-column-list">
                <div className="section-heading">
                  <h2>SharePoint uploads</h2>
                  <p>Files produced by the latest pipeline run and their upload status.</p>
                </div>

                <div className="filter-row">
                  <label className="filter-label" htmlFor="upload-filter">
                    Quick filter
                  </label>
                  <input
                    id="upload-filter"
                    type="text"
                    value={uploadFilter}
                    onChange={(event) => setUploadFilter(event.target.value)}
                  />
                </div>

                {dashboard.uploads.length === 0 ? (
                  <div className="table-empty">No uploads recorded for the latest run.</div>
                ) : filteredUploads.length === 0 ? (
                  <div className="table-empty">No uploads match the current filter.</div>
                ) : (
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          {uploadColumns.map((column) => {
                            const isActive = uploadSortConfig.key === column.key;
                            const sortIndicator = isActive ? (uploadSortConfig.direction === "asc" ? "↑" : "↓") : "↕";

                            return (
                              <th key={column.key}>
                                <button
                                  type="button"
                                  className={`sort-button${isActive ? " sort-button-active" : ""}`}
                                  onClick={() => handleUploadSort(column.key)}
                                >
                                  <span>{column.label}</span>
                                  <span className="sort-indicator">{sortIndicator}</span>
                                </button>
                              </th>
                            );
                          })}
                        </tr>
                      </thead>
                      <tbody>
                        {sortedUploads.map((item) => (
                          <tr key={item.id}>
                            <td data-label="File">{item.file_name}</td>
                            <td data-label="Folder">{item.folder}</td>
                            <td data-label="Status">
                              <span className={`status-tag status-tag-${String(item.status || "unknown").toLowerCase()}`}>
                                {item.status || "N/A"}
                              </span>
                            </td>
                            <td data-label="Link">
                              {item.web_url ? (
                                <a className="sharepoint-link-button" href={item.web_url} target="_blank" rel="noreferrer">
                                  <span className="sharepoint-link-mark" aria-hidden="true">
                                  </span>
                                  Open file
                                </a>
                              ) : (
                                "N/A"
                              )}
                            </td>
                            <td data-label="Uploaded">{formatDate(item.uploaded_at)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>
            </article>
          </section>
        ) : null}

        {activeDashboardTab.startsWith("powerbi:") ? (
          embeddedReports
            .filter((report) => `powerbi:${report.reportId}` === activeDashboardTab)
            .map((report) => (
              <EmbeddedPowerBIReport
                key={report.reportId}
                report={report}
                onManageDashboards={() => setCurrentView("settings")}
              />
            ))
        ) : null}
      </section>

      <BrandingFooter />
    </main>
  );
}

export default App;
