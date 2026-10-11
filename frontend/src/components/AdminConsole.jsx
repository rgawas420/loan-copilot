import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import AdminBilling from "./AdminBilling.jsx";
import AdminPage from "./AdminPage.jsx";
import KnowledgeBase from "./KnowledgeBase.jsx";
import RagQuality from "./RagQuality.jsx";
import DataQuality from "./DataQuality.jsx";
import AdsAdmin from "./AdsAdmin.jsx";
import Icon from "./Icon.jsx";

const TABS = [
  { id: "overview", label: "Overview", icon: "pulse" },
  { id: "users", label: "Users", icon: "users" },
  { id: "billing", label: "Billing & usage", icon: "card" },
  { id: "kb", label: "Knowledge base", icon: "book" },
  { id: "rag", label: "RAG quality", icon: "shield" },
  { id: "data", label: "Data quality", icon: "sheet" },
  { id: "ads", label: "Ads", icon: "spark" },
  { id: "activity", label: "Activity", icon: "spark" },
  { id: "policies", label: "Policies and audit", icon: "folder" },
];

const ACTIONS = {
  register: "Signed up", login: "Logged in", logout: "Logged out", password_changed: "Changed password",
  conversation_created: "Started a chat", conversation_renamed: "Renamed a chat", conversation_deleted: "Deleted a chat",
  intake: "Described a case", recommendation: "Ran a recommendation", chat: "Asked a follow-up",
  kb_question: "Asked the knowledge base", admin_created_user: "Created a user", admin_updated_user: "Updated a user",
  admin_deleted_user: "Deleted a user", kb_upload: "Uploaded documents", kb_reprocess: "Reprocessed documents",
  kb_sync: "Synced the knowledge base", kb_delete: "Deleted documents", kb_archive: "Archived documents",
  kb_restore: "Restored documents", kb_metadata: "Edited document tags", kb_version_activated: "Re-activated a document version",
  rag_golden_saved: "Saved a golden test", rag_golden_deleted: "Deleted a golden test", rag_golden_run: "Ran golden tests",
  ad_created: "Created an ad", ad_updated: "Updated an ad", ad_deleted: "Deleted an ad",
  checkout_started: "Started a checkout", trial_started: "Started a free trial", plan_downgrade_scheduled: "Scheduled a downgrade",
  subscription_canceled: "Canceled a subscription", admin_assigned_plan: "Assigned a plan", admin_plan_saved: "Saved a plan",
  admin_price_set: "Set a model price", admin_billing_settings: "Changed billing settings", admin_charge_updated: "Updated a charge",
  admin_rebuilt_usage_totals: "Recomputed usage counters", attachment: "Attached a file",
};

const when = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "Never");

function Stat({ label, value }) {
  return <div className="stat"><span>{label}</span><b>{value ?? "–"}</b></div>;
}

function ActivityTable({ rows, showUser = true }) {
  if (!rows.length) return <p className="empty">No activity yet.</p>;
  return (
    <div className="table-wrap">
      <table className="table">
        <thead><tr><th>Time</th>{showUser && <th>User</th>}<th>Action</th><th>Detail</th></tr></thead>
        <tbody>
          {rows.map((a) => (
            <tr key={a.id}>
              <td className="nowrap">{when(a.ts)}</td>
              {showUser && <td>{a.name ? <>{a.name}<small className="block muted">{a.email}</small></> : <span className="muted">Deleted user</span>}</td>}
              <td>{ACTIONS[a.action] || a.action}</td>
              <td className="muted">{a.detail}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Overview() {
  const [stats, setStats] = useState(null);
  const [activity, setActivity] = useState([]);
  const [error, setError] = useState("");
  useEffect(() => {
    Promise.all([api.adminStats(), api.adminActivity()])
      .then(([s, a]) => { setStats(s); setActivity(a.activity.slice(0, 15)); })
      .catch((e) => setError(e.message));
  }, []);
  return (
    <>
      {error && <p className="err" role="alert">{error}</p>}
      <div className="stats">
        <Stat label="Users" value={stats && `${stats.active_users} active / ${stats.users}`} />
        <Stat label="Admins" value={stats?.admins} />
        <Stat label="Conversations" value={stats?.conversations} />
        <Stat label="Active users (24h)" value={stats?.active_users_24h} />
        <Stat label="Logins (24h)" value={stats?.logins_24h} />
        <Stat label="Recommendations (24h)" value={stats?.recommendations_24h} />
        <Stat label="Questions asked (24h)" value={stats?.questions_24h} />
      </div>
      <section className="panel">
        <h2>Recent activity</h2>
        <ActivityTable rows={activity} />
      </section>
    </>
  );
}

function NewUser({ onCreated }) {
  const [open, setOpen] = useState(false);
  const [f, setF] = useState({ name: "", email: "", password: "" });
  const [msg, setMsg] = useState("");
  const set = (k) => (e) => setF((x) => ({ ...x, [k]: e.target.value }));
  async function submit(e) {
    e.preventDefault();
    try {
      const r = await api.adminCreateUser(f.email, f.name, f.password);
      setMsg(`Created ${r.user.email}. Share the password with them securely.`);
      setF({ name: "", email: "", password: "" });
      onCreated();
    } catch (err) { setMsg(err.message); }
  }
  if (!open) return <button className="btn btn-primary btn-sm" onClick={() => setOpen(true)}>Add user</button>;
  return (
    <form className="panel inline-form" onSubmit={submit}>
      <h3>Add user</h3>
      <div className="form-grid">
        <label>Name<input type="text" required maxLength={80} value={f.name} onChange={set("name")} /></label>
        <label>Email<input type="email" required maxLength={200} value={f.email} onChange={set("email")} /></label>
        <label>Temporary password<input type="text" required minLength={8} maxLength={200} value={f.password}
                                        autoComplete="off" onChange={set("password")} /></label>
      </div>
      <div className="row-gap">
        <button className="btn btn-primary btn-sm" type="submit">Create user</button>
        <button className="btn btn-ghost btn-sm" type="button" onClick={() => { setOpen(false); setMsg(""); }}>Cancel</button>
      </div>
      <p className="hint" role="status">{msg}</p>
    </form>
  );
}

function UserRow({ u, me, onChanged, onActivity }) {
  const [mode, setMode] = useState(null);       // null | "password" | "delete"
  const [pw, setPw] = useState("");
  const [err, setErr] = useState("");
  const self = u.id === me.id;

  async function run(fn) {
    setErr("");
    try { await fn(); setMode(null); setPw(""); onChanged(); }
    catch (e) { setErr(e.message); }
  }

  return (
    <>
      <tr className={u.active ? "" : "inactive"}>
        <td><b>{u.name}</b>{self && <span className="tag">You</span>}<small className="block muted">{u.email}</small></td>
        <td>
          <label className="sr-only" htmlFor={`role-${u.id}`}>Role for {u.email}</label>
          <select id={`role-${u.id}`} value={u.role} disabled={self}
                  onChange={(e) => run(() => api.adminUpdateUser(u.id, { role: e.target.value }))}>
            <option value="user">User</option><option value="admin">Admin</option>
          </select>
        </td>
        <td><span className={`tag ${u.active ? "ok" : "off"}`}>{u.active ? "Active" : "Disabled"}</span></td>
        <td className="num">{u.conversations}</td>
        <td className="nowrap">{when(u.last_login)}</td>
        <td className="nowrap">{when(u.created)}</td>
        <td className="actions">
          <button className="link small" onClick={() => onActivity(u)}>Activity</button>
          {!self && (
            <button className="link small" onClick={() => run(() => api.adminUpdateUser(u.id, { active: !u.active }))}>
              {u.active ? "Disable" : "Enable"}
            </button>
          )}
          <button className="link small" onClick={() => setMode(mode === "password" ? null : "password")}>Reset password</button>
          {!self && <button className="link small danger-text" onClick={() => setMode("delete")}>Delete</button>}
        </td>
      </tr>
      {(mode || err) && (
        <tr className="row-detail">
          <td colSpan={7}>
            {mode === "password" && (
              <form className="row-gap" onSubmit={(e) => { e.preventDefault(); run(() => api.adminUpdateUser(u.id, { password: pw })); }}>
                <label className="sr-only" htmlFor={`pw-${u.id}`}>New password</label>
                <input id={`pw-${u.id}`} type="text" minLength={8} required placeholder="New password (8+ characters)"
                       autoComplete="off" value={pw} onChange={(e) => setPw(e.target.value)} />
                <button className="btn btn-primary btn-sm" type="submit">Set password</button>
                <button className="btn btn-ghost btn-sm" type="button" onClick={() => setMode(null)}>Cancel</button>
                <span className="muted small">This also signs them out everywhere.</span>
              </form>
            )}
            {mode === "delete" && (
              <div className="row-gap">
                <span>Delete {u.email} and all {u.conversations} of their chats? This cannot be undone.</span>
                <button className="btn btn-danger btn-sm" onClick={() => run(() => api.adminDeleteUser(u.id))}>Delete user</button>
                <button className="btn btn-ghost btn-sm" onClick={() => setMode(null)}>Cancel</button>
              </div>
            )}
            {err && <p className="err" role="alert">{err}</p>}
          </td>
        </tr>
      )}
    </>
  );
}

function Users({ me, onActivity }) {
  const [users, setUsers] = useState(null);
  const [q, setQ] = useState("");
  const [error, setError] = useState("");
  const load = useCallback(() => api.adminUsers().then((r) => setUsers(r.users)).catch((e) => setError(e.message)), []);
  useEffect(() => { load(); }, [load]);

  const shown = (users || []).filter((u) => `${u.name} ${u.email}`.toLowerCase().includes(q.trim().toLowerCase()));
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Users {users && <span className="count">{users.length}</span>}</h2>
        <input type="search" className="search" placeholder="Search users" aria-label="Search users" value={q}
               onChange={(e) => setQ(e.target.value)} />
        <NewUser onCreated={load} />
      </div>
      {error && <p className="err" role="alert">{error}</p>}
      {users && (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>User</th><th>Role</th><th>Status</th><th className="num">Chats</th><th>Last login</th><th>Joined</th><th /></tr></thead>
            <tbody>{shown.map((u) => <UserRow key={u.id} u={u} me={me} onChanged={load} onActivity={onActivity} />)}</tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Activity({ user, onClear }) {
  const [rows, setRows] = useState(null);
  const [error, setError] = useState("");
  const load = useCallback(() => {
    api.adminActivity(user?.id).then((r) => setRows(r.activity)).catch((e) => setError(e.message));
  }, [user]);
  useEffect(() => { load(); }, [load]);
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>{user ? `Activity: ${user.name}` : "All activity"}</h2>
        {user && <button className="btn btn-ghost btn-sm" onClick={onClear}>Show everyone</button>}
        <button className="btn btn-ghost btn-sm" onClick={load}>Refresh</button>
      </div>
      <p className="hint">The activity log records what happened and when. It never stores case details or customer data.</p>
      {error && <p className="err" role="alert">{error}</p>}
      {rows && <ActivityTable rows={rows} showUser={!user} />}
    </section>
  );
}

export default function AdminConsole({ me, sidebarOpen, onOpenSidebar }) {
  const [tab, setTab] = useState("overview");
  const [activityUser, setActivityUser] = useState(null);
  return (
    <div className="chat-main admin-main">
      <header className="topbar">
        {!sidebarOpen && (
          <button className="icon-btn" aria-label="Open sidebar" onClick={onOpenSidebar}><Icon name="menu" size={20} /></button>
        )}
        <h1 className="top-title">Admin console</h1>
      </header>
      <div className="admin-scroll">
        <div className="admin-wrap">
          <nav className="seg" aria-label="Admin sections">
            {TABS.map((t) => (
              <button key={t.id} className={tab === t.id ? "on" : ""} aria-current={tab === t.id ? "page" : undefined}
                      onClick={() => setTab(t.id)}><Icon name={t.icon} size={16} />{t.label}</button>
            ))}
          </nav>
          {tab === "overview" && <Overview />}
          {tab === "users" && <Users me={me} onActivity={(u) => { setActivityUser(u); setTab("activity"); }} />}
          {tab === "billing" && <AdminBilling />}
          {tab === "kb" && <KnowledgeBase />}
          {tab === "rag" && <RagQuality />}
          {tab === "data" && <DataQuality />}
          {tab === "ads" && <AdsAdmin />}
          {tab === "activity" && <Activity user={activityUser} onClear={() => setActivityUser(null)} />}
          {tab === "policies" && <AdminPage />}
        </div>
      </div>
    </div>
  );
}
