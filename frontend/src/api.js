// Thin client for the FastAPI backend. Every function throws an Error with a readable message.
// The login session is an HttpOnly cookie, sent automatically on same-origin requests.

let onUnauthorized = () => {};
export const setUnauthorizedHandler = (fn) => { onUnauthorized = fn; };

/** An Error from a failed response. A structured detail ({code, message, ...}) is kept as err.code / err.data,
 *  e.g. code "quota_exceeded" with the plan, limit and usage. */
function httpError(status, detail) {
  const err = new Error(typeof detail === "string" ? detail : detail?.message || `Request failed (${status})`);
  err.status = status;
  if (detail && typeof detail === "object" && !Array.isArray(detail)) { err.code = detail.code; err.data = detail; }
  return err;
}

async function request(path, { body, method, headers } = {}) {
  const res = await fetch(path, {
    method: method || (body !== undefined ? "POST" : "GET"),
    credentials: "same-origin",
    headers: { ...(body !== undefined && !(body instanceof FormData) ? { "Content-Type": "application/json" } : {}), ...headers },
    body: body instanceof FormData ? body : body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && !path.startsWith("/api/auth/")) onUnauthorized();
  if (!res.ok) throw httpError(res.status, data.detail);
  return data;
}

const adminHeaders = (key) => (key ? { "X-Admin-Key": key } : {});

export const api = {
  health: () => request("/api/health"),
  next: (caseData) => request("/api/questionnaire/next", { body: { case: caseData } }),
  answer: (caseData, key, value, skip = false) =>
    request("/api/questionnaire/answer", { body: { case: caseData, key, value: skip ? null : String(value), skip } }),
  intake: (caseData, text, conversationId) =>
    request("/api/intake", { body: { case: caseData, text, conversation_id: conversationId || null } }),
  pin: (pin) => request(`/api/pin/${encodeURIComponent(pin)}`),
  loadCase: (caseId, token) =>
    request(`/api/case/${encodeURIComponent(caseId)}`, { headers: { "X-Case-Token": token || "" } }),
  chat: (caseData, result, question, history) =>
    request("/api/chat", { body: { case: caseData, result, question, history } }),
  askKb: (question, loanType, history) =>
    request("/api/kb/ask", { body: { question, loan_type: loanType || null, history } }),

  // accounts
  me: () => request("/api/auth/me"),
  authConfig: () => request("/api/auth/config"),
  login: (email, password) => request("/api/auth/login", { body: { email, password } }),
  googleLogin: (credential) => request("/api/auth/google", { body: { credential } }),
  register: (email, name, password) => request("/api/auth/register", { body: { email, name, password } }),
  logout: () => request("/api/auth/logout", { method: "POST" }),
  changePassword: (current, next) => request("/api/auth/password", { body: { current, new: next } }),

  // conversations (the server only ever returns the logged-in user's own)
  conversations: (q = "", archived = false) =>
    request(`/api/conversations?q=${encodeURIComponent(q)}${archived ? "&archived=true" : ""}`),
  conversation: (id) => request(`/api/conversations/${encodeURIComponent(id)}`),
  createConversation: (title, data) => request("/api/conversations", { body: { title, data } }),
  updateConversation: (id, patch) =>
    request(`/api/conversations/${encodeURIComponent(id)}`, { method: "PATCH", body: patch }),
  deleteConversation: (id) => request(`/api/conversations/${encodeURIComponent(id)}`, { method: "DELETE" }),
  feedback: (conversationId, messageId, rating, reason = null, comment = "") =>
    request("/api/feedback", { body: { conversation_id: conversationId, message_id: messageId, rating, reason, comment } }),
  uploadAttachment: (file) => {
    const fd = new FormData();
    fd.append("file", file, file.name);
    return request("/api/attachments", { body: fd });
  },
  deleteAttachment: (id) => request(`/api/attachments/${encodeURIComponent(id)}`, { method: "DELETE" }),

  // usage and billing (always the logged-in user's own)
  usage: () => request("/api/usage/current"),
  usageDaily: (range = "7d") => request(`/api/usage/daily?range=${range}`),
  usageByModel: (range = "period") => request(`/api/usage/by-model?range=${range}`),
  usageHistory: (offset = 0, limit = 25) => request(`/api/usage/history?offset=${offset}&limit=${limit}`),
  plans: () => request("/api/plans"),
  subscription: () => request("/api/subscription"),
  checkout: (planId) => request("/api/subscription/checkout", { body: { plan_id: planId } }),
  startTrial: (planId) => request("/api/subscription/trial", { body: { plan_id: planId } }),
  downgrade: (planId) => request("/api/subscription/downgrade", { body: { plan_id: planId } }),
  cancelSubscription: () => request("/api/subscription/cancel", { method: "POST" }),
  verifyCheckout: (checkoutId, payload) => request("/api/subscription/verify", { body: { checkout_id: checkoutId, payload } }),
  mockComplete: (checkoutId, outcome) => request("/api/subscription/mock/complete", { body: { checkout_id: checkoutId, outcome } }),

  // admin: billing and usage
  billingOverview: () => request("/api/admin/billing/overview"),
  billingDaily: () => request("/api/admin/billing/daily"),
  billingUsers: (q = "") => request(`/api/admin/billing/users?q=${encodeURIComponent(q)}`),
  billingUser: (id) => request(`/api/admin/billing/users/${id}`),
  billingAssignPlan: (id, planId) => request(`/api/admin/billing/users/${id}/plan`, { body: { plan_id: planId } }),
  billingLedger: (filters = {}) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(filters)) if (v !== "" && v != null) qs.set(k, v);
    return request(`/api/admin/billing/ledger?${qs}`);
  },
  billingPlans: () => request("/api/admin/billing/plans"),
  billingSavePlan: (plan, id) =>
    request(id ? `/api/admin/billing/plans/${id}` : "/api/admin/billing/plans", { method: id ? "PUT" : "POST", body: plan }),
  billingPrices: () => request("/api/admin/billing/prices"),
  billingSetPrice: (p) => request("/api/admin/billing/prices", { body: p }),
  billingSettings: () => request("/api/admin/billing/settings"),
  billingSaveSettings: (s) => request("/api/admin/billing/settings", { method: "PUT", body: s }),
  billingPayments: () => request("/api/admin/billing/payments"),
  billingChargeStatus: (id, status) => request(`/api/admin/billing/charges/${id}`, { method: "PATCH", body: { status } }),
  billingRebuildTotals: () => request("/api/admin/billing/rebuild-totals", { method: "POST" }),
  billingMockEvent: (subId, kind) => request(`/api/admin/billing/mock/subscriptions/${subId}`, { body: { kind } }),

  // admin console (admin role)
  adminStats: () => request("/api/admin/stats"),
  adminUsers: () => request("/api/admin/users"),
  adminCreateUser: (email, name, password) => request("/api/admin/users", { body: { email, name, password } }),
  adminUpdateUser: (id, patch) => request(`/api/admin/users/${id}`, { method: "PATCH", body: patch }),
  adminDeleteUser: (id) => request(`/api/admin/users/${id}`, { method: "DELETE" }),
  adminActivity: (userId) => request(`/api/admin/activity?limit=200${userId ? `&user_id=${userId}` : ""}`),

  // knowledge-base documents (admin role)
  kb: () => request("/api/admin/kb"),
  kbProcess: (paths) => request("/api/admin/kb/process", { body: { paths } }),
  kbSyncFolder: (full = false) => request("/api/admin/kb/sync", { body: { full } }),
  kbDelete: (paths) => request("/api/admin/kb/delete", { body: { paths } }),
  kbArchive: (paths) => request("/api/admin/kb/archive", { body: { paths } }),
  kbRestore: (paths) => request("/api/admin/kb/restore", { body: { paths } }),
  kbDeleteArchived: (paths) => request("/api/admin/kb/archived/delete", { body: { paths } }),
  kbPreview: (path) => request(`/api/admin/kb/preview?path=${encodeURIComponent(path)}`),
  kbSearch: (query, loanType) => request("/api/admin/kb/search", { body: { query, loan_type: loanType || null } }),
  kbDocument: (path) => request(`/api/admin/kb/document?path=${encodeURIComponent(path)}`),
  kbUpdateDocument: (path, fields) => request("/api/admin/kb/document", { method: "PATCH", body: { path, ...fields } }),
  kbActivateVersion: (path, version) => request("/api/admin/kb/versions/activate", { body: { path, version } }),
  kbDecide: (id, body) => request(`/api/admin/kb/pending/${encodeURIComponent(id)}/decide`, { body }),
  kbPendingPreview: (id) => request(`/api/admin/kb/pending/${encodeURIComponent(id)}/preview`),
  kbPendingReview: (id, docId) =>
    request(`/api/admin/kb/pending/${encodeURIComponent(id)}/review?doc_id=${encodeURIComponent(docId)}`),
  kbVersionCompare: (path, version) =>
    request(`/api/admin/kb/versions/compare?path=${encodeURIComponent(path)}&version=${version}`),
  kbHistorySearch: (query) => request("/api/admin/kb/history-search", { body: { query } }),
  kbVerifyGet: ({ path, pendingId }) =>
    request(`/api/admin/kb/verify?${pendingId ? `pending_id=${encodeURIComponent(pendingId)}` : `path=${encodeURIComponent(path)}`}`),
  kbVerifySave: ({ path, pendingId }, text, { checked = [], baseAt = null, acceptWarnings = false } = {}) =>
    request("/api/admin/kb/verify", { body: { path: path || null, pending_id: pendingId || null, text, checked,
                                              base_at: baseAt, accept_warnings: acceptWarnings } }),
  kbVerifyRecheck: ({ path, pendingId }) =>
    request("/api/admin/kb/verify/recheck", { body: { path: path || null, pending_id: pendingId || null } }),
  kbVerifyPageUrl: ({ path, pendingId }, page) =>
    `/api/admin/kb/verify/page?page=${page}&${pendingId ? `pending_id=${encodeURIComponent(pendingId)}` : `path=${encodeURIComponent(path)}`}`,
  kbVerifyRemove: ({ path, pendingId }) =>
    request("/api/admin/kb/verify/remove", { body: { path: path || null, pending_id: pendingId || null } }),
  kbPendingFileUrl: (id) => `/api/admin/kb/pending/${encodeURIComponent(id)}/file`,
  kbAudit: (params) => request(`/api/admin/kb/audit?${new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== "" && v != null))}`),
  kbDuplicates: () => request("/api/admin/kb/duplicates"),
  bankerQuality: (limit = 20) => request(`/api/admin/kb/banker-quality?limit=${limit}`),
  aliases: () => request("/api/admin/kb/aliases"),
  aliasAdd: (kind, alias, canonical) => request("/api/admin/kb/aliases", { body: { kind, alias, canonical } }),
  aliasRemove: (kind, alias) => request("/api/admin/kb/aliases/remove", { body: { kind, alias } }),
  facts: (status = "") => request(`/api/admin/kb/facts?status=${encodeURIComponent(status)}`),
  factsImport: (file) => { const fd = new FormData(); fd.append("file", file); return request("/api/admin/kb/facts/import", { body: fd }); },
  factsApprove: (ids) => request("/api/admin/kb/facts/approve", { body: { ids } }),
  factsDelete: (ids) => request("/api/admin/kb/facts/delete", { body: { ids } }),
  kbMerge: (keep, others) => request("/api/admin/kb/merge", { body: { keep, others } }),
  kbVersionPreview: (path, version) =>
    request(`/api/admin/kb/versions/preview?path=${encodeURIComponent(path)}&version=${version}`),
  kbDownloadUrl: (path, archived = false) =>
    `/api/admin/kb/download?path=${encodeURIComponent(path)}${archived ? "&archived=true" : ""}`,

  // sponsored cards
  adHide: (id) => request(`/api/ads/${id}/hide`, { method: "POST" }),
  adReport: (id, reason, note = "") => request(`/api/ads/${id}/report`, { body: { reason, note } }),
  adminAds: () => request("/api/admin/ads"),
  adminAdSave: (ad, id) => request(id ? `/api/admin/ads/${id}` : "/api/admin/ads", { method: id ? "PUT" : "POST", body: ad }),
  adminAdActive: (id, active) => request(`/api/admin/ads/${id}`, { method: "PATCH", body: { active } }),
  adminAdDelete: (id) => request(`/api/admin/ads/${id}`, { method: "DELETE" }),
  adminAdImage: (id, file) => { const fd = new FormData(); fd.append("file", file, file.name); return request(`/api/admin/ads/${id}/image`, { body: fd }); },
  adminAdReport: (id) => request(`/api/admin/ads/${id}/report`),

  // RAG quality (admin role): traces, test bench, golden tests, configuration
  ragConfig: () => request("/api/admin/rag/config"),
  policyCoverage: (minRms = 1) => request(`/api/admin/rag/policy-coverage?min_rms=${minRms}`),
  ragTraces: (q = "", only = "") =>
    request(`/api/admin/rag/traces?q=${encodeURIComponent(q)}&only=${encodeURIComponent(only)}`),
  ragTrace: (id, expect = {}) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(expect)) if (v !== "" && v != null) qs.set(k, v);
    return request(`/api/admin/rag/traces/${encodeURIComponent(id)}?${qs}`);
  },
  ragTest: (body) => request("/api/admin/rag/test", { body }),
  ragGolden: () => request("/api/admin/rag/golden"),
  ragGoldenSave: (item, id) =>
    request(id ? `/api/admin/rag/golden/${id}` : "/api/admin/rag/golden", { method: id ? "PUT" : "POST", body: item }),
  ragGoldenDelete: (id) => request(`/api/admin/rag/golden/${id}`, { method: "DELETE" }),
  ragGoldenRun: () => request("/api/admin/rag/golden/run", { method: "POST" }),
  ragRun: (id) => request(`/api/admin/rag/golden/runs/${id}`),
  ragRunResume: (id) => request(`/api/admin/rag/golden/runs/${id}/resume`, { method: "POST" }),
  ragRunBaseline: (id) => request(`/api/admin/rag/golden/runs/${id}/baseline`, { method: "POST" }),
  ragFeedback: (status = "", rating = "") =>
    request(`/api/admin/rag/feedback?status=${encodeURIComponent(status)}&rating=${encodeURIComponent(rating)}`),
  ragFeedbackReview: (id, status, note = "") =>
    request(`/api/admin/rag/feedback/${id}`, { method: "PUT", body: { status, note } }),
  ragFeedbackGolden: (id, item) => request(`/api/admin/rag/feedback/${id}/golden`, { body: item }),

  // policies and audit (admin session, or the admin key)
  ingest: (adminKey, file, lender, loanType) => {
    const fd = new FormData();
    fd.append("file", file); fd.append("lender", lender || ""); fd.append("loan_type", loanType || "");
    return request("/api/ingest", { body: fd, headers: adminHeaders(adminKey) });
  },
  policies: (adminKey) => request("/api/policies", { headers: adminHeaders(adminKey) }),
  deleteDoc: (adminKey, id) =>
    request(`/api/policies/doc/${encodeURIComponent(id)}`, { method: "DELETE", headers: adminHeaders(adminKey) }),
  audit: (adminKey) => request("/api/audit", { headers: adminHeaders(adminKey) }),
  kbStatus: () => request("/api/kb/status"),
  kbSync: (full = false) => request(`/api/kb/sync?full=${full}`, { method: "POST" }),
};

// Multi-file upload with progress (fetch cannot report upload progress). onProgress gets 0..1.
/** Stage files for checking. target: path of the document they update (optional). */
export function uploadKbFiles(files, folder, target, onProgress) {
  return new Promise((resolve, reject) => {
    const fd = new FormData();
    for (const f of files) fd.append("files", f, f.name);
    fd.append("folder", folder || "");
    fd.append("target", target || "");
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/admin/kb/upload");
    xhr.withCredentials = true;
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress?.(e.loaded / e.total); };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
      if (xhr.status === 401) onUnauthorized();
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error(typeof data.detail === "string" ? data.detail : `Upload failed (${xhr.status})`));
    };
    xhr.onerror = () => reject(new Error("Upload failed: the connection was interrupted."));
    xhr.send(fd);
  });
}

/** Read a server-sent-event stream from a POST; calls onEvent(kind, data) for each event. */
async function readEvents(path, body, onEvent, signal) {
  let res;
  try {
    res = await fetch(path, {
      method: "POST", credentials: "same-origin", signal,
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
  } catch (e) {
    if (e.name === "AbortError") throw e;
    const err = new Error("Could not reach the server. Check your connection and try again.");
    err.network = true;
    throw err;
  }
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    if (res.status === 401) onUnauthorized();
    throw httpError(res.status, d.detail);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    let chunk;
    try { chunk = await reader.read(); }
    catch (e) {
      if (e.name === "AbortError" || signal?.aborted) { const a = new Error("Stopped"); a.name = "AbortError"; throw a; }
      const err = new Error("The connection was interrupted.");
      err.network = true;
      throw err;
    }
    if (chunk.done) break;
    buffer += decoder.decode(chunk.value, { stream: true });
    let i;
    while ((i = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, i);
      buffer = buffer.slice(i + 2);
      const kind = (block.match(/^event: (.*)$/m) || [])[1];
      const data = JSON.parse((block.match(/^data: (.*)$/m) || [])[1] || "null");
      if (onEvent(kind, data) === false) return;
    }
  }
}

/**
 * Streamed AI answer: status steps, then tokens as the model writes them, then (if the answer failed the source
 * check and was rewritten) the whole corrected answer, then the cited sources and, if a figure in the answer was
 * not found in those sources, a verification warning.
 * Resolves when the server sends 'done'. Rejects with .network when the stream breaks off,
 * with .server for an error the server reported, or with an AbortError when stopped.
 */
export async function streamChat(body, { onStatus, onToken, onReplace, onSources, onVerification, onConfidence, onState, onPlaces, onUsage, onAd, signal }) {
  let done = false;
  await readEvents("/api/chat/stream", body, (kind, data) => {
    if (kind === "status") onStatus?.(data);
    else if (kind === "token") onToken?.(data);
    else if (kind === "replace") onReplace?.(data);
    else if (kind === "sources") onSources?.(data);
    else if (kind === "verification") onVerification?.(data);
    else if (kind === "confidence") onConfidence?.(data);    // high / medium / low, after the claim check
    else if (kind === "state") onState?.(data);              // {state, retry, notice, actions} (app/rag/states.py)
    else if (kind === "places") onPlaces?.(data);          // lender branches for a map (customer profiles)
    else if (kind === "ad") onAd?.(data);
    else if (kind === "usage") onUsage?.(data);
    else if (kind === "done") { done = true; return false; }
    else if (kind === "error") {
      const err = new Error(data?.message || "Something went wrong.");
      err.server = true;
      err.state = data?.state;                               // RATE_LIMITED / TEMPORARY_SERVICE_FAILURE / INTERNAL_ERROR
      throw err;
    }
    return true;
  }, signal);
  if (!done) {
    const err = new Error("The connection was interrupted.");
    err.network = true;
    throw err;
  }
}

// Recommendation agent: calls onStep(line) for each agent step and resolves with the final result.
export async function streamRecommend(caseData, caseId, onStep, signal, onUsage) {
  let result = null;
  await readEvents("/api/recommend/stream", { case: caseData, case_id: caseId }, (kind, data) => {
    if (kind === "step") onStep(data);
    else if (kind === "usage") onUsage?.(data);
    else if (kind === "result") result = data;
    else if (kind === "error") throw new Error(data?.message || "Recommendation failed.");
    return true;
  }, signal);
  if (!result) throw new Error("The connection closed before a result arrived. Try again.");
  return result;
}
