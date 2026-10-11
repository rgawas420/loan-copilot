import { Suspense, lazy, useCallback, useEffect, useRef, useState } from "react";
import { api, setUnauthorizedHandler } from "./api.js";
import { useConversation } from "./useConversation.js";
import AuthScreen from "./components/AuthScreen.jsx";
import ChatView from "./components/ChatView.jsx";
import PasswordDialog from "./components/PasswordDialog.jsx";
import Sidebar from "./components/Sidebar.jsx";

// Routes live in the hash: #/ (new chat), #/c/<id> (a saved chat), #/billing (usage & billing), #/admin (admin console).
function parseRoute(hash = window.location.hash) {
  const m = hash.match(/^#\/c\/([A-Za-z0-9]+)$/);
  if (m) return { page: "chat", id: m[1] };
  if (hash === "#/admin" || hash === "#admin") return { page: "admin" };
  if (hash === "#/billing") return { page: "billing" };
  return { page: "chat", id: null };
}

// Links from the earlier version: /?case=ID#t=TOKEN open that saved case in a new chat.
function readLegacyLink() {
  const caseId = new URLSearchParams(window.location.search).get("case");
  const token = new URLSearchParams(window.location.hash.slice(1)).get("t");
  return caseId && token ? { caseId, token } : null;
}

// Admin and billing screens load on demand: partners never download the admin console.
const AdminConsole = lazy(() => import("./components/AdminConsole.jsx"));
const BillingPage = lazy(() => import("./components/BillingPage.jsx"));
const PageLoading = () => <main className="chat-main"><p className="muted page-loading" role="status">Loading…</p></main>;

const isNarrow = () => window.matchMedia("(max-width: 900px)").matches;
const byUpdated = (a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0) || (b.updated || "").localeCompare(a.updated || "");

function Workspace({ user, onLogout }) {
  const [route, setRoute] = useState(parseRoute);
  const [conversations, setConversations] = useState([]);
  const [listLoading, setListLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [archivedView, setArchivedView] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(() => !isNarrow());
  const [narrow, setNarrow] = useState(isNarrow);
  const drawer = narrow && sidebarOpen;           // the sidebar is a modal drawer over the page
  const [pwOpen, setPwOpen] = useState(false);
  const [health, setHealth] = useState({ text: "Checking service", ok: null });
  const [usage, setUsage] = useState(null);
  // The server's usage summary: set from a response that carries it, or fetched again (null).
  const refreshUsage = useCallback((u) => {
    if (u) setUsage(u);
    else api.usage().then(setUsage).catch(() => {});
  }, []);
  useEffect(() => { refreshUsage(null); }, [refreshUsage]);
  const queryRef = useRef(query);
  queryRef.current = query;
  const archivedRef = useRef(archivedView);
  archivedRef.current = archivedView;

  const upsert = useCallback((summary) => {
    if (!summary?.id) return;
    setConversations((list) => {
      const others = list.filter((c) => c.id !== summary.id);
      const prev = list.find((c) => c.id === summary.id);
      // A chat archived (or unarchived) leaves this list, even if a late autosave reports it afterwards.
      if (summary.archived !== undefined && summary.archived !== archivedRef.current) return others;
      if (!prev && (queryRef.current || archivedRef.current)) return list;   // not into a filtered list
      return [{ ...prev, ...summary }, ...others].sort(byUpdated);
    });
  }, []);

  const onCreated = useCallback((conv, isCurrent) => {
    upsert({ id: conv.id, title: conv.title, created: conv.created, updated: conv.updated });
    if (isCurrent) {
      window.history.replaceState(null, "", `${window.location.pathname}#/c/${conv.id}`);
      setRoute({ page: "chat", id: conv.id });
    }
  }, [upsert]);

  const conv = useConversation({ onCreated, onSaved: upsert, onUsage: refreshUsage });
  const { open, newChat } = conv;

  // Route changes (sidebar clicks, back/forward) open the matching chat.
  const handleRoute = useCallback(async (r) => {
    if (r.page === "admin" && user.role !== "admin") r = { page: "chat", id: null };
    setRoute(r);
    if (r.page !== "chat") return;
    if (r.id) { open(r.id); return; }
    const legacy = readLegacyLink();
    if (legacy) {
      window.history.replaceState(null, "", "/#/");
      try {
        const saved = await api.loadCase(legacy.caseId, legacy.token);
        newChat(saved.case, "I opened your saved case. Here is where we left off.");
        return;
      } catch { /* fall through to a blank chat */ }
    }
    newChat();
  }, [open, newChat, user.role]);

  useEffect(() => {
    handleRoute(parseRoute());
    const onHash = () => handleRoute(parseRoute());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, [handleRoute]);

  useEffect(() => {                             // the sidebar is an overlay on narrow screens: close it when shrinking
    const mq = window.matchMedia("(max-width: 900px)");
    const onChange = (e) => { setSidebarOpen(!e.matches); setNarrow(e.matches); };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  // The drawer is modal: focus moves into it, Escape closes it, and focus goes back to the menu button.
  const wasDrawer = useRef(false);
  useEffect(() => {
    if (drawer) {
      document.querySelector(".sidebar button")?.focus({ preventScroll: true });
      const onKey = (e) => { if (e.key === "Escape" && !e.defaultPrevented && !document.querySelector("dialog[open], .menu")) setSidebarOpen(false); };
      document.addEventListener("keydown", onKey);
      wasDrawer.current = true;
      return () => document.removeEventListener("keydown", onKey);
    }
    if (wasDrawer.current) {
      wasDrawer.current = false;
      if (!document.activeElement || document.activeElement === document.body) {
        document.querySelector('[aria-label="Open sidebar"]')?.focus({ preventScroll: true });
      }
    }
    return undefined;
  }, [drawer]);

  useEffect(() => {
    api.health()
      .then((h) => setHealth((h.llm_configured ?? h.groq_configured)
        ? { text: `Ready: ${h.structured_programs} programs, ${h.vector_store} knowledge base`, ok: true }
        : { text: `Rules only: add ${h.llm_provider === "openai" ? "OPENAI_API_KEY" : "GROQ_API_KEY"} for explanations, free-text intake and chat`, ok: false }))
      .catch(() => setHealth({ text: "Service offline", ok: false }));
  }, []);

  useEffect(() => {                             // conversation list, filtered by the search box
    let live = true;
    const t = setTimeout(() => {
      setListLoading(true);
      api.conversations(query.trim(), archivedView)
        .then((r) => live && setConversations(r.conversations))
        .catch(() => {})
        .finally(() => live && setListLoading(false));
    }, query ? 250 : 0);
    return () => { live = false; clearTimeout(t); };
  }, [query, archivedView]);

  function go(hash) {
    if (isNarrow()) setSidebarOpen(false);
    if (window.location.hash === hash) handleRoute(parseRoute(hash));
    else window.location.hash = hash;
  }

  async function remove(id) {
    await api.deleteConversation(id);
    conv.forget(id);
    setConversations((list) => list.filter((c) => c.id !== id));
    if (route.page === "chat" && route.id === id) go("#/");
  }

  async function rename(id, title) {
    upsert(await conv.rename(id, title));
  }

  async function pin(id, pinned) {
    upsert(await api.updateConversation(id, { pinned }));
  }

  async function archive(id, archived) {
    upsert(await api.updateConversation(id, { archived }));
    if (archived && route.page === "chat" && route.id === id) go("#/");
  }

  const active = conversations.find((c) => c.id === conv.id);
  const title = active?.title || (conv.id ? "Chat" : "New chat");
  const currentSummary = conv.id ? active || { id: conv.id, title, pinned: false } : null;

  return (
    <div className={`app${sidebarOpen ? " side-open" : ""}`}>
      {sidebarOpen && (
        <>
          <div className="scrim side-scrim" onClick={() => setSidebarOpen(false)} />
          <Sidebar user={user} page={route.page} conversations={conversations} loading={listLoading} activeId={conv.id}
                   query={query} onQuery={setQuery} onNew={() => go("#/")} onOpen={(id) => go(`#/c/${id}`)}
                   onRename={rename} onDelete={remove} onPin={pin} onArchive={archive}
                   archivedView={archivedView} onArchivedView={(v) => { setArchivedView(v); setQuery(""); }}
                   onAdmin={() => go("#/admin")} onBilling={() => go("#/billing")} usage={usage}
                   onPassword={() => setPwOpen(true)}
                   onLogout={onLogout} onCollapse={() => setSidebarOpen(false)} />
        </>
      )}
      <div className="main-slot" inert={drawer || undefined}>
      <Suspense fallback={<PageLoading />}>
      {route.page === "admin" && user.role === "admin"
        ? <AdminConsole me={user} sidebarOpen={sidebarOpen} onOpenSidebar={() => setSidebarOpen(true)} />
        : route.page === "billing"
        ? <BillingPage sidebarOpen={sidebarOpen} onOpenSidebar={() => setSidebarOpen(true)} onUsageChanged={refreshUsage} />
        : <ChatView c={conv} conv={currentSummary} title={title} health={health} isAdmin={user.role === "admin"}
                    usage={usage} onViewUsage={() => go("#/billing")}
                    sidebarOpen={sidebarOpen} onOpenSidebar={() => setSidebarOpen(true)} onNewChat={() => go("#/")}
                    onRename={(t) => rename(conv.id, t)} onPin={(p) => pin(conv.id, p)}
                    onArchive={(a) => archive(conv.id, a)} onDelete={() => remove(conv.id)} />}
      </Suspense>
      </div>
      {pwOpen && <PasswordDialog onClose={() => setPwOpen(false)} />}
    </div>
  );
}

export default function App() {
  const [user, setUser] = useState(undefined);       // undefined = checking the session, null = logged out

  useEffect(() => {
    setUnauthorizedHandler(() => setUser(null));
    api.me().then((r) => setUser(r.user)).catch(() => setUser(null));
  }, []);

  async function logout() {
    try { await api.logout(); } catch { /* the session is gone either way */ }
    window.history.replaceState(null, "", "/");
    setUser(null);
  }

  if (user === undefined) return <div className="splash"><span className="logo xl" aria-hidden="true">₹</span></div>;
  if (!user) return <AuthScreen onAuthed={setUser} />;
  return <Workspace key={user.id} user={user} onLogout={logout} />;
}
