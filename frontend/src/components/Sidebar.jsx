import { useEffect, useRef, useState } from "react";
import Icon from "./Icon.jsx";
import { UsageMeter } from "./Usage.jsx";
import { useInstallPrompt } from "../pwa.js";

function groupOf(iso) {
  const d = new Date(iso), now = new Date();
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(now) - day(d)) / 86400000);
  if (diff <= 0) return "Today";
  if (diff === 1) return "Yesterday";
  if (diff < 7) return "Previous 7 days";
  if (diff < 30) return "Previous 30 days";
  return d.toLocaleString(undefined, { month: "long", year: "numeric" });
}

function useOutside(ref, onOutside, active) {
  useEffect(() => {
    if (!active) return;
    const h = (e) => { if (ref.current && !ref.current.contains(e.target)) onOutside(); };
    const k = (e) => { if (e.key === "Escape") onOutside(); };
    document.addEventListener("mousedown", h);
    document.addEventListener("keydown", k);
    return () => { document.removeEventListener("mousedown", h); document.removeEventListener("keydown", k); };
  }, [ref, onOutside, active]);
}

function ChatItem({ conv, active, archivedView, onOpen, onRename, onDelete, onPin, onArchive }) {
  const [menu, setMenu] = useState(false);
  const [mode, setMode] = useState(null);           // null | "rename" | "delete"
  const [title, setTitle] = useState(conv.title);
  const [error, setError] = useState("");
  const ref = useRef(null);
  useOutside(ref, () => setMenu(false), menu);

  async function rename(e) {
    e?.preventDefault();
    const t = title.trim();
    if (!t || t === conv.title) { setMode(null); setTitle(conv.title); return; }
    try { await onRename(conv.id, t); setMode(null); setError(""); }
    catch (err) { setError(err.message); }
  }

  if (mode === "rename") {
    return (
      <li className="chat-item editing">
        <form onSubmit={rename}>
          <label className="sr-only" htmlFor={`rn-${conv.id}`}>Chat name</label>
          <input id={`rn-${conv.id}`} autoFocus maxLength={120} value={title} onChange={(e) => setTitle(e.target.value)}
                 onBlur={rename} onKeyDown={(e) => { if (e.key === "Escape") { setMode(null); setTitle(conv.title); } }} />
        </form>
        {error && <p className="side-error">{error}</p>}
      </li>
    );
  }
  if (mode === "delete") {
    return (
      <li className="chat-item confirming">
        <p>Delete “{conv.title}”?</p>
        <div className="confirm-row">
          <button className="btn btn-danger btn-sm" onClick={async () => {
            try { await onDelete(conv.id); } catch (err) { setError(err.message); setMode(null); }
          }}>Delete</button>
          <button className="btn btn-ghost btn-sm" onClick={() => setMode(null)}>Cancel</button>
        </div>
      </li>
    );
  }
  return (
    <li className={`chat-item${active ? " active" : ""}${menu ? " menu-open" : ""}`} ref={ref}>
      <a href={`#/c/${conv.id}`} onClick={(e) => { e.preventDefault(); onOpen(conv.id); }}
         aria-current={active ? "page" : undefined} title={conv.title}>
        {conv.pinned && <Icon name="pin" size={13} className="pin-mark" />}{conv.title}
      </a>
      <button className="icon-btn item-more" aria-label={`Options for ${conv.title}`} aria-haspopup="menu"
              aria-expanded={menu} onClick={() => setMenu((m) => !m)}><Icon name="more" /></button>
      {menu && (
        <div className="menu" role="menu">
          {!archivedView && (
            <button role="menuitem" onClick={() => { setMenu(false); onPin(conv.id, !conv.pinned).catch((e) => setError(e.message)); }}>
              <Icon name="pin" />{conv.pinned ? "Unpin" : "Pin"}
            </button>
          )}
          <button role="menuitem" onClick={() => { setMenu(false); setTitle(conv.title); setMode("rename"); }}><Icon name="edit" />Rename</button>
          <button role="menuitem" onClick={() => { setMenu(false); onArchive(conv.id, !archivedView).catch((e) => setError(e.message)); }}>
            <Icon name="archive" />{archivedView ? "Unarchive" : "Archive"}
          </button>
          <button role="menuitem" className="danger-text" onClick={() => { setMenu(false); setMode("delete"); }}>
            <Icon name="trash" />Delete
          </button>
        </div>
      )}
      {error && <p className="side-error">{error}</p>}
    </li>
  );
}

function UserMenu({ user, page, onAdmin, onChat, onPassword, onLogout, onBilling }) {
  const [open, setOpen] = useState(false);
  const [iosHelp, setIosHelp] = useState(false);
  const { canInstall, iosHint, install } = useInstallPrompt();
  const ref = useRef(null);
  useOutside(ref, () => setOpen(false), open);
  const initials = user.name.split(/\s+/).map((p) => p[0]).join("").slice(0, 2).toUpperCase();
  return (
    <div className="user-menu" ref={ref}>
      {open && (
        <div className="menu up" role="menu">
          <div className="menu-head"><b>{user.name}</b><span>{user.email}</span></div>
          {user.role === "admin" && (page === "admin"
            ? <button role="menuitem" onClick={() => { setOpen(false); onChat(); }}><Icon name="compose" />Back to chats</button>
            : <button role="menuitem" onClick={() => { setOpen(false); onAdmin(); }}><Icon name="shield" />Admin console</button>)}
          <button role="menuitem" onClick={() => { setOpen(false); onBilling(); }}><Icon name="card" />Usage &amp; billing</button>
          <button role="menuitem" onClick={() => { setOpen(false); onPassword(); }}><Icon name="key" />Change password</button>
          {canInstall && (
            <button role="menuitem" onClick={() => { setOpen(false); install(); }}><Icon name="download" />Install app</button>
          )}
          {iosHint && (
            <button role="menuitem" onClick={() => setIosHelp((v) => !v)} aria-expanded={iosHelp}>
              <Icon name="download" />Install app
            </button>
          )}
          {iosHint && iosHelp && (
            <p className="menu-note">In Safari, tap <b>Share</b>, then <b>Add to Home Screen</b>.</p>
          )}
          <button role="menuitem" onClick={onLogout}><Icon name="logout" />Log out</button>
        </div>
      )}
      <button className="user-btn" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <span className="avatar">{initials}</span>
        <span className="user-name"><span>{user.name}</span>{user.role === "admin" && <small>Admin</small>}</span>
      </button>
    </div>
  );
}

export default function Sidebar({ user, page, conversations, loading, activeId, query, onQuery, onNew, onOpen, onRename,
                                  onDelete, onPin, onArchive, archivedView, onArchivedView, onAdmin, onPassword,
                                  onLogout, onCollapse, onBilling, usage }) {
  const groups = [];
  for (const c of conversations) {
    const g = c.pinned && !archivedView ? "Pinned" : groupOf(c.updated);
    if (!groups.length || groups.at(-1).name !== g) groups.push({ name: g, items: [] });
    groups.at(-1).items.push(c);
  }

  return (
    <nav className="sidebar" aria-label="Chats">
      <div className="side-top">
        <button className="icon-btn" aria-label="Close sidebar" title="Close sidebar" onClick={onCollapse}>
          <Icon name="sidebar" size={20} />
        </button>
        <button className="icon-btn" aria-label="New chat" title="New chat" onClick={onNew}><Icon name="compose" size={20} /></button>
      </div>

      <button className="side-new" onClick={onNew}>
        <span className="logo" aria-hidden="true">₹</span><span>New chat</span><Icon name="compose" />
      </button>
      {user.role === "admin" && (
        <button className={`side-link${page === "admin" ? " active" : ""}`} onClick={onAdmin}>
          <Icon name="shield" /><span>Admin console</span>
        </button>
      )}

      <div className="side-search">
        <Icon name="search" />
        <label htmlFor="chat-search" className="sr-only">Search chats</label>
        <input id="chat-search" type="search" placeholder="Search chats" value={query} onChange={(e) => onQuery(e.target.value)} />
      </div>

      <div className="side-list">
        {loading && !conversations.length && <p className="side-empty">Loading chats…</p>}
        {archivedView && (
          <button className="side-back" onClick={() => onArchivedView(false)}><Icon name="back" size={16} />Archived chats</button>
        )}
        {!loading && !conversations.length && (
          <p className="side-empty">{query ? "No chats match your search." : archivedView ? "No archived chats." : "Your chats will appear here."}</p>
        )}
        {groups.map((g) => (
          <section key={g.name}>
            <h2>{g.name}</h2>
            <ul>
              {g.items.map((c) => (
                <ChatItem key={c.id} conv={c} active={page === "chat" && c.id === activeId} archivedView={archivedView}
                          onOpen={onOpen} onRename={onRename} onDelete={onDelete} onPin={onPin} onArchive={onArchive} />
              ))}
            </ul>
          </section>
        ))}
        {!archivedView && (
          <button className="side-archived" onClick={() => onArchivedView(true)}><Icon name="archive" size={15} />Archived chats</button>
        )}
      </div>

      <UsageMeter usage={usage} onOpen={onBilling} />
      <UserMenu user={user} page={page} onAdmin={onAdmin} onChat={onNew} onPassword={onPassword} onLogout={onLogout}
                onBilling={onBilling} />
    </nav>
  );
}
