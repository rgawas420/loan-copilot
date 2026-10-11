(() => {
  const $ = (s) => document.querySelector(s);
  const params = new URLSearchParams(location.search);
  const S = {
    case: {}, stack: [], q: null, ready: false, result: null, chat: [],
    caseId: params.get("case"), token: new URLSearchParams(location.hash.slice(1)).get("t"),
  };

  const h = (tag, attrs = {}, ...kids) => {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) k === "class" ? (el.className = v) : el.setAttribute(k, v);
    kids.flat().forEach((c) => el.append(c instanceof Node ? c : document.createTextNode(c ?? "")));
    return el;
  };
  const label = (k) => k.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

  async function api(path, body, opts = {}) {
    const res = await fetch(path, {
      method: opts.method || (body ? "POST" : "GET"),
      headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...(opts.headers || {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Request failed (${res.status})`);
    return data;
  }

  /* ---------- question flow ---------- */
  function apply(r) {
    if (r.case) S.case = r.case;
    S.q = r.question; S.ready = r.ready;
    $("#bar").style.width = `${Math.round((r.answered / Math.max(r.total, 1)) * 100)}%`;
    $("#ready").hidden = !r.ready;
    $("#ready-msg").textContent = r.message;
    renderLedger(r.summary);
    renderQuestion();
  }

  const loadNext = async () => apply(await api("/api/questionnaire/next", { case: S.case }));

  function renderQuestion() {
    const q = S.q, input = $("#qinput"), opts = $("#qopts");
    opts.replaceChildren(); $("#qhint").textContent = ""; $("#qerr").textContent = ""; $("#savemsg").textContent = "";
    $("#btn-back").disabled = S.stack.length === 0;
    if (!q) {
      $("#qtext").textContent = "All applicable questions are answered or skipped.";
      input.hidden = true; $("#btn-next").hidden = true; $("#btn-skip").hidden = true; $("#qreq").hidden = true;
      return;
    }
    $("#qtext").textContent = q.text;
    $("#qreq").hidden = !q.required;
    $("#btn-skip").hidden = q.required;
    if (q.options) {
      input.hidden = true; $("#btn-next").hidden = true;
      q.options.forEach((o) => opts.append(h("button", { type: "button", class: S.case[q.key] === o ? "sel" : "" }, o)));
      opts.firstChild.focus();
    } else {
      input.hidden = false; $("#btn-next").hidden = false;
      input.value = S.case[q.key] ?? "";
      input.placeholder = q.placeholder || "";
      input.inputMode = ["money", "pin", "mobile", "age", "years", "count", "percent", "year", "cibil"].includes(q.kind) ? "decimal" : "text";
      input.focus();
    }
  }

  async function answer(value, skip = false) {
    if (!S.q) return;
    const key = S.q.key;
    try {
      const r = await api("/api/questionnaire/answer", { case: S.case, key, value: skip ? null : String(value), skip });
      S.stack.push(key);
      apply(r);
    } catch (e) { $("#qerr").textContent = e.message; }
  }

  function forget(key) {
    delete S.case[key];
    if (S.case._skipped) S.case._skipped = S.case._skipped.filter((k) => k !== key);
  }

  $("#qopts").addEventListener("click", (e) => { if (e.target.tagName === "BUTTON") answer(e.target.textContent); });
  $("#qform").addEventListener("submit", (e) => { e.preventDefault(); answer($("#qinput").value); });
  $("#btn-skip").addEventListener("click", () => answer(null, true));
  $("#btn-back").addEventListener("click", () => { const k = S.stack.pop(); if (k) { forget(k); loadNext(); } });

  // PIN auto-fill hint
  $("#qinput").addEventListener("input", async (e) => {
    if (!S.q || S.q.kind !== "pin" || !/^\d{6}$/.test(e.target.value)) return;
    try { const p = await api(`/api/pin/${e.target.value}`); $("#qhint").textContent = `${p.city}, ${p.district}, ${p.state}`; }
    catch { $("#qhint").textContent = ""; }
  });

  function renderLedger(items) {
    const dl = $("#ledger"); dl.replaceChildren();
    $("#sheet-count").textContent = `${items.filter((i) => !i.skipped).length} answered`;
    if (!items.length) { dl.append(h("p", { class: "empty" }, "Answers appear here as you go.")); return; }
    items.forEach((it) => {
      const edit = h("button", { class: "edit", type: "button", "aria-label": `Change ${it.label}` }, "Change");
      edit.onclick = () => { forget(it.key); S.stack = S.stack.filter((k) => k !== it.key); loadNext(); };
      dl.append(h("div", { class: it.skipped ? "row skipped" : "row" },
        h("dt", {}, it.label), h("span", { class: "lead" }), h("dd", {}, it.value, edit)));
    });
  }

  /* ---------- free-text intake ---------- */
  $("#btn-intake").addEventListener("click", async () => {
    const text = $("#intake-text").value.trim(), msg = $("#intake-msg"), btn = $("#btn-intake");
    if (text.length < 3) { msg.textContent = "Write a short description first."; return; }
    btn.disabled = true; msg.textContent = "Reading the description";
    try {
      const r = await api("/api/intake", { case: S.case, text });
      apply(r);
      const bad = r.rejected.map((x) => `${label(x.key)} (${x.reason})`);
      msg.textContent = `Filled ${r.filled.length} answers. Check them in the case sheet.` +
        (bad.length ? ` Not used: ${bad.join("; ")}.` : "");
      if (r.filled.length) $("#intake").open = false;
    } catch (e) { msg.textContent = e.message; }
    finally { btn.disabled = false; }
  });

  /* ---------- save & resume ---------- */
  $("#btn-save").addEventListener("click", async () => {
    try {
      const r = await api("/api/case/save", { case: S.case, case_id: S.caseId, token: S.token });
      S.caseId = r.case_id; S.token = r.token;
      const link = `${location.origin}/?case=${r.case_id}#t=${r.token}`;
      history.replaceState({}, "", link);
      $("#savemsg").textContent = `Saved. Resume with this private link (anyone who has it can open the case): ${link}`;
    } catch (e) { $("#savemsg").textContent = e.message; }
  });

  $("#btn-new").addEventListener("click", () => {
    Object.assign(S, { case: {}, stack: [], caseId: null, token: null, result: null, chat: [] });
    $("#result").hidden = true; $("#chat").hidden = true; $("#intake").open = true; $("#intake-text").value = "";
    $("#intake-msg").textContent = "";
    history.replaceState({}, "", "/"); loadNext();
  });

  /* ---------- recommendation (streamed) ---------- */
  const badgeClass = (s) => ({ "Strong Match": "b-strong", "Possible - Credit Check Required": "b-possible", "Not Suitable": "b-not" }[s] || "b-more");
  const money = (n) => (typeof n === "number" ? "₹" + n.toLocaleString("en-IN") : "Not computed");
  const MARK = { pass: "✓", fail: "✗", deviation: "!", unknown: "?", warn: "i" };
  const MARK_LABEL = { pass: "Pass", fail: "Fails", deviation: "Deviation", unknown: "Not known", warn: "Note" };

  async function streamRecommend(onStep) {
    const res = await fetch("/api/recommend/stream", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ case: S.case, case_id: S.caseId }),
    });
    if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || `Request failed (${res.status})`); }
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = "", result = null;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, i); buf = buf.slice(i + 2);
        const ev = (block.match(/^event: (.*)$/m) || [])[1];
        const data = JSON.parse((block.match(/^data: (.*)$/m) || [])[1] || "null");
        if (ev === "step") onStep(data);
        else if (ev === "result") result = data;
        else if (ev === "error") throw new Error(data.message);
      }
    }
    if (!result) throw new Error("The connection closed before a result arrived. Try again.");
    return result;
  }

  $("#btn-find").addEventListener("click", async () => {
    const btn = $("#btn-find"), box = $("#result");
    btn.disabled = true; btn.textContent = "Searching lenders";
    $("#chat").hidden = true;
    const steps = h("ol");
    box.hidden = false;
    box.replaceChildren(h("p", { class: "hint", role: "status" }, "Checking lender policies."),
      h("div", { class: "trace" }, h("h3", {}, "How the agent is deciding"), steps));
    try {
      renderResult(await streamRecommend((line) => steps.append(h("li", {}, line))));
    } catch (e) { box.replaceChildren(h("p", { class: "warn", role: "alert" }, e.message)); }
    finally { btn.disabled = false; btn.textContent = "Find suitable banks"; box.scrollIntoView({ behavior: "smooth" }); }
  });

  function list(title, items, empty = "None noted") {
    return h("div", {}, h("h3", {}, title), h("ul", {}, (items && items.length ? items : [empty]).map((x) => h("li", {}, x))));
  }

  function comparisonTable(rows) {
    const body = h("tbody");
    rows.forEach((r, i) => {
      const n = (res) => r.checks.filter((c) => c.result === res).length;
      const detail = h("tr", { class: "detail", hidden: "" }, h("td", { colspan: "4" },
        h("ul", { class: "checks" }, r.checks.map((c) =>
          h("li", {}, h("span", { class: `chk chk-${c.result}`, title: MARK_LABEL[c.result], "aria-label": MARK_LABEL[c.result] }, MARK[c.result]),
            h("b", {}, c.criterion), " ", c.detail)))));
      const toggle = h("button", { type: "button", class: "linkish", "aria-expanded": "false" }, "Show checks");
      toggle.onclick = () => {
        detail.hidden = !detail.hidden;
        toggle.setAttribute("aria-expanded", String(!detail.hidden));
        toggle.textContent = detail.hidden ? "Show checks" : "Hide checks";
      };
      body.append(
        h("tr", { class: i === 0 ? "best" : "" },
          h("td", {}, h("b", {}, r.lender), h("br"), r.program, r.sample ? h("span", { class: "sample" }, " sample") : ""),
          h("td", {}, h("span", { class: `badge small ${badgeClass(r.status)}` }, r.status)),
          h("td", {}, money(r.max_loan)),
          h("td", {}, `${n("pass")} pass, ${n("fail")} fail, ${n("deviation") + n("unknown")} open `, toggle)),
        detail);
    });
    return h("div", { class: "cmp-wrap" }, h("table", { class: "cmp" },
      h("caption", {}, "Every program checked for this loan type"),
      h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Lender and program"), h("th", { scope: "col" }, "Status"),
        h("th", { scope: "col" }, "Indicative maximum"), h("th", { scope: "col" }, "Checks"))), body));
  }

  function renderResult(r) {
    S.result = r; S.chat = [];
    const box = $("#result"); box.replaceChildren();
    box.append(h("span", { class: `badge ${badgeClass(r.status)}` }, r.status));
    if (r.top_lender) box.append(h("h2", {}, `${r.top_lender}: ${r.program}`));
    if (r.message) box.append(h("p", {}, r.message));
    if (r.missing_fields) box.append(h("p", { class: "hint" }, "Still needed: " + r.missing_fields.map(label).join(", ")));
    if (r.summary) box.append(h("p", { class: "summary" }, r.summary));
    if (r.evidence_source === "private_kb")
      box.append(h("p", { class: "warn" }, "Matched from policy text only: this lender has no structured rules yet. Verify the criteria with the RM."));
    if (r.evidence_source === "web_general") {
      box.append(h("p", { class: "warn" }, "General guidance from public web results. No lender is recommended because none in the knowledge base covers this loan type."));
      if (r.web_summary) box.append(h("p", {}, r.web_summary));
      box.append(list("Typical criteria", r.general_criteria));
    }
    if (r.top_lender) {
      const e = r.eligibility_calc || {};
      box.append(h("dl", { class: "facts" },
        h("dt", {}, "Indicative eligibility"), h("dd", {}, r.indicative_eligibility),
        h("dt", {}, "Calculated maximum"), h("dd", {}, `${money(e.indicative_max_loan)} against ${money(e.requested)} requested`),
        h("dt", {}, "Banker or RM"), h("dd", {}, r.banker_rm),
        h("dt", {}, "Mobile"), h("dd", {}, r.mobile),
        h("dt", {}, "Urban Money code"), h("dd", {}, r.urban_money_code),
        h("dt", {}, "Policy last verified"), h("dd", {}, r.policy_last_verified)));
    }
    if (r.top_lender || r.concerns_or_deviations?.length) {
      box.append(h("div", { class: "cols" },
        r.top_lender ? list("Why suitable", r.why_suitable) : "",
        list("Concerns and deviations", r.concerns_or_deviations),
        r.next_steps ? list("Next steps", r.next_steps) : "",
        r.top_lender ? list("Alternative lenders", (r.alternative_lenders || []).map((a) => `${a.lender}: ${a.program}${a.status ? ` (${a.status})` : ""}`)) : ""));
    }
    if (r.comparison?.length) box.append(comparisonTable(r.comparison));
    if (r.trace) box.append(h("div", { class: "trace" }, h("h3", {}, "How the agent decided"), h("ol", {}, r.trace.map((t) => h("li", {}, t)))));
    box.append(h("p", { class: "disclaimer" }, "Indicative only. This is not a sanction. The lender makes the final decision."));
    showChat(r);
  }

  /* ---------- follow-up chat ---------- */
  function showChat(r) {
    if (r.status === "More Information Required" && !r.comparison) { $("#chat").hidden = true; return; }
    $("#chat").hidden = false; $("#chat-log").replaceChildren();
    const sug = $("#chat-suggest"); sug.replaceChildren();
    ["Why is this the best fit?", "Why not the other lenders?", "What would make this a Strong Match?"]
      .forEach((q) => sug.append(h("button", { type: "button" }, q)));
  }

  async function ask(question) {
    question = question.trim();
    if (!question || !S.result) return;
    const log = $("#chat-log"), input = $("#chat-input");
    log.append(h("li", { class: "me" }, question));
    const pending = h("li", { class: "bot", role: "status" }, "Thinking");
    log.append(pending); input.value = ""; input.disabled = true;
    try {
      const r = await api("/api/chat", { case: S.case, result: S.result, question, history: S.chat });
      pending.textContent = r.answer;
      S.chat.push({ role: "user", text: question }, { role: "assistant", text: r.answer });
    } catch (e) { pending.textContent = e.message; pending.className = "bot err"; }
    finally { input.disabled = false; input.focus(); }
  }

  $("#chat-form").addEventListener("submit", (e) => { e.preventDefault(); ask($("#chat-input").value); });
  $("#chat-suggest").addEventListener("click", (e) => { if (e.target.tagName === "BUTTON") ask(e.target.textContent); });

  /* ---------- upload + health ---------- */
  $("#btn-upload").addEventListener("click", async () => {
    const f = $("#policyfile").files[0], msg = $("#uploadmsg");
    if (!f) { msg.textContent = "Choose a file first."; return; }
    const fd = new FormData();
    fd.append("file", f); fd.append("lender", $("#up-lender").value); fd.append("loan_type", $("#up-type").value);
    msg.textContent = "Uploading";
    try {
      const res = await fetch("/api/ingest", { method: "POST", headers: { "X-Admin-Key": $("#adminkey").value }, body: fd });
      const d = await res.json().catch(() => ({}));
      if (!res.ok) { msg.textContent = d.detail || `Upload failed (${res.status})`; return; }
      msg.textContent = d.kind === "structured"
        ? `Loaded ${d.programs} structured programs for ${d.lender}.`
        : `Added ${d.chunks_indexed} policy sections from ${d.filename}.`;
    } catch { msg.textContent = "Upload failed. Check your connection."; }
  });

  async function boot() {
    try {
      const hl = await api("/api/health"), p = $("#health");
      p.textContent = hl.groq_configured ? `Ready (${hl.structured_programs} programs, ${hl.vector_store} KB)`
        : `Rules only: add GROQ_API_KEY for explanations and chat`;
      p.className = "pill " + (hl.groq_configured ? "ok" : "bad");
    } catch { $("#health").textContent = "Service offline"; $("#health").className = "pill bad"; }
    if (S.caseId) {
      try {
        S.case = (await api(`/api/case/${S.caseId}`, null, { headers: { "X-Case-Token": S.token || "" } })).case;
        $("#intake").open = false;
      } catch (e) { $("#intake-msg").textContent = e.message; S.caseId = null; S.token = null; }
    }
    loadNext();
  }
  boot();
})();
