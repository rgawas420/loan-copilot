import { createContext, memo, useContext, useMemo, useState } from "react";
import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import css from "highlight.js/lib/languages/css";
import java from "highlight.js/lib/languages/java";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import python from "highlight.js/lib/languages/python";
import sql from "highlight.js/lib/languages/sql";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";
import Icon from "./Icon.jsx";

// Markdown for AI answers, rendered progressively while the answer streams in.
// The text is split into blocks and each block is memoised, so only the block being written re-renders.
// Builds React elements only; the one HTML string (code highlighting) comes from highlight.js, which escapes it.

for (const [name, lang] of Object.entries({ bash, css, java, javascript, json, python, sql, typescript, xml, yaml })) {
  hljs.registerLanguage(name, lang);
}
const ALIAS = { js: "javascript", jsx: "javascript", ts: "typescript", tsx: "typescript", sh: "bash", shell: "bash",
  html: "xml", py: "python", yml: "yaml", console: "bash" };
const COLLAPSE_LINES = 40;

// ---------------------------------------------------------------- blocks
const FENCE = /^\s*(```|~~~)\s*([\w+#.-]*)\s*$/;
const HEADING = /^(#{1,6})\s+(.*)$/;
const BULLET = /^(\s*)[-*+•]\s+(.*)$/;
const NUMBER = /^(\s*)(\d+)[.)]\s+(.*)$/;
const QUOTE = /^\s*>\s?(.*)$/;
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
const isTableRule = (l) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l || "");
const isTableRow = (l) => /^\s*\|.*\|\s*$/.test(l || "") || ((l || "").includes("|") && (l || "").trim().startsWith("|"));

export function parseBlocks(src) {
  const lines = (src || "").replace(/\r/g, "").split("\n");
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }

    const fence = line.match(FENCE);
    if (fence) {
      const body = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(fence[1])) body.push(lines[i++]);
      const open = i >= lines.length;           // still streaming: no closing fence yet
      if (!open) i++;
      blocks.push({ type: "code", lang: fence[2].toLowerCase(), code: body.join("\n"), open });
      continue;
    }
    const h = line.match(HEADING);
    if (h) { blocks.push({ type: "h", level: Math.min(h[1].length + 1, 5), text: h[2] }); i++; continue; }
    if (RULE.test(line)) { blocks.push({ type: "hr" }); i++; continue; }
    if (isTableRow(line) && isTableRule(lines[i + 1])) {
      const rows = [line];
      i += 2;
      while (i < lines.length && isTableRow(lines[i])) rows.push(lines[i++]);
      blocks.push({ type: "table", rows });
      continue;
    }
    if (QUOTE.test(line)) {
      const body = [];
      while (i < lines.length && QUOTE.test(lines[i])) body.push(lines[i++].match(QUOTE)[1]);
      blocks.push({ type: "quote", text: body.join("\n") });
      continue;
    }
    if (BULLET.test(line) || NUMBER.test(line)) {
      const ordered = !BULLET.test(line);
      const same = ordered ? NUMBER : BULLET;              // a bullet list ends where a numbered list starts
      const items = [];
      while (i < lines.length) {
        const l = lines[i];
        const top = l.match(same);
        const nested = /^\s{2,}/.test(l) && (l.match(BULLET) || l.match(NUMBER));
        const m = top || nested;
        if (m) {
          const indent = m[1].length;
          const text = m[3] ?? m[2];
          if (indent >= 2 && items.length) items.at(-1).children.push(text);
          else items.push({ text, start: m[3] !== undefined ? Number(m[2]) : null, children: [] });
          i++;
        } else if (l.trim() && /^\s{2,}/.test(l) && items.length) {     // wrapped continuation line
          items.at(-1).text += " " + l.trim();
          i++;
        } else if (!l.trim() && same.test(lines[i + 1] || "")) {
          i++;                                                         // blank line inside a loose list
        } else break;
      }
      blocks.push({ type: ordered ? "ol" : "ul", items });
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !FENCE.test(lines[i]) && !HEADING.test(lines[i]) &&
           !BULLET.test(lines[i]) && !NUMBER.test(lines[i]) && !QUOTE.test(lines[i]) &&
           !(isTableRow(lines[i]) && isTableRule(lines[i + 1]))) {
      para.push(lines[i++]);
    }
    blocks.push({ type: "p", text: para.join("\n") });
  }
  return blocks;
}

// ---------------------------------------------------------------- inline
const INLINE = /(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|\[[^\]\n]+\]\((?:https?:\/\/|mailto:)[^)\s]+\)|\[[^\]\n]{1,140}\](?!\()|\*[^*\s][^*\n]*\*|(?<![\w])_[^_\s][^_\n]*_(?![\w])|https?:\/\/[^\s)<>\]]+)/g;

// onCite("S1"): set by an answer whose sources can be opened (admins), so each [S1] opens its passage.
const CiteContext = createContext(null);

export function Inline({ text }) {
  const onCite = useContext(CiteContext);
  const parts = (text || "").split(INLINE);
  return parts.map((p, i) => {
    if (onCite && /^\[S\d+\]$/.test(p)) {
      const ref = p.slice(1, -1);
      return <button key={i} type="button" className="cite cite-link" onClick={() => onCite(ref)}
                     title={`Show source ${ref}`} aria-label={`Show source ${ref}`}>{ref}</button>;
    }
    if (!p) return null;
    if (/^`[^`]+`$/.test(p)) return <code key={i}>{p.slice(1, -1)}</code>;
    if (/^(\*\*|__).+(\*\*|__)$/.test(p)) return <strong key={i}><Inline text={p.slice(2, -2)} /></strong>;
    const link = p.match(/^\[([^\]]+)\]\(((?:https?:\/\/|mailto:)[^)\s]+)\)$/);
    if (link) return <a key={i} href={link[2]} target="_blank" rel="noopener noreferrer">{link[1]}</a>;
    if (/^\[[^\]]+\]$/.test(p)) return <span key={i} className="cite">{p.slice(1, -1)}</span>;
    if (/^(\*[^*].*\*|_[^_].*_)$/.test(p)) return <em key={i}>{p.slice(1, -1)}</em>;
    if (/^https?:\/\//.test(p)) return <a key={i} href={p} target="_blank" rel="noopener noreferrer">{p}</a>;
    return p;
  });
}

const Lines = ({ text }) => text.split("\n").map((l, i) => <span key={i}>{i > 0 && <br />}<Inline text={l} /></span>);
const cells = (row) => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());

// ---------------------------------------------------------------- code
function CodeBlock({ lang, code, open }) {
  const [copied, setCopied] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const language = ALIAS[lang] || lang;
  const html = useMemo(() => {
    try {
      if (language && hljs.getLanguage(language)) return hljs.highlight(code, { language, ignoreIllegals: true }).value;
      if (!open && code.length < 20000) return hljs.highlightAuto(code).value;    // only once it is complete
    } catch { /* fall back to plain text */ }
    return null;
  }, [code, language, open]);
  const lines = code.split("\n").length;
  const collapsible = !open && lines > COLLAPSE_LINES;
  const copy = () => navigator.clipboard?.writeText(code).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1600); });
  return (
    <div className={`code-block${collapsible && !expanded ? " collapsed" : ""}`}>
      <div className="code-head">
        <span>{lang || "code"}</span>
        <button type="button" className="code-copy" onClick={copy} aria-label={copied ? "Copied" : "Copy code"} disabled={open}>
          <Icon name={copied ? "check" : "copy"} size={14} />{copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre>{html != null ? <code className="hljs" dangerouslySetInnerHTML={{ __html: html }} /> : <code>{code}</code>}</pre>
      {collapsible && (
        <button type="button" className="code-more" onClick={() => setExpanded((e) => !e)}>
          {expanded ? "Show less" : `Show all ${lines} lines`}
        </button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- block renderer (memoised)
const Cursor = () => <span className="cursor" aria-hidden="true" />;

// tail: this is the block being written, so the streaming cursor goes at the end of its text.
const Block = memo(function Block({ b, tail }) {
  const cur = tail ? <Cursor /> : null;
  switch (b.type) {
    case "code": return <><CodeBlock lang={b.lang} code={b.code} open={b.open} />{cur}</>;
    case "h": { const H = `h${b.level}`; return <H className="md-h"><Inline text={b.text} />{cur}</H>; }
    case "hr": return <><hr />{cur}</>;
    case "quote": return <blockquote><Lines text={b.text} />{cur}</blockquote>;
    case "table": {
      const [head, ...rows] = b.rows.map(cells);
      return (
        <div className="md-table">
          <table>
            <thead><tr>{head.map((c, j) => <th key={j}><Inline text={c} /></th>)}</tr></thead>
            <tbody>{rows.map((r, ri) => <tr key={ri}>{r.map((c, j) => <td key={j}><Inline text={c} /></td>)}</tr>)}</tbody>
          </table>
          {cur}
        </div>
      );
    }
    case "ul": case "ol": {
      const L = b.type;
      return (
        <L start={b.type === "ol" && b.items[0]?.start > 1 ? b.items[0].start : undefined}>
          {b.items.map((it, j) => {
            const last = j === b.items.length - 1;
            return (
              <li key={j}><Inline text={it.text} />{last && !it.children.length && cur}
                {it.children.length > 0 && (
                  <ul>{it.children.map((c, k) => (
                    <li key={k}><Inline text={c} />{last && k === it.children.length - 1 && cur}</li>
                  ))}</ul>
                )}
              </li>
            );
          })}
        </L>
      );
    }
    default: return <p><Lines text={b.text} />{cur}</p>;
  }
}, (a, b) => a.tail === b.tail && JSON.stringify(a.b) === JSON.stringify(b.b));

export default function Markdown({ text, streaming = false, onCite = null }) {
  // Some models cite with full-width brackets (【S1】), labels ([Source: S1]) or spaces ([ S1 ]); show them as [S1].
  const blocks = useMemo(() => parseBlocks((text || "")
    .replace(/【\s*([SAW]\d+)[^】]{0,20}】/g, "[$1]")
    .replace(/\[\s*sources?\s*:\s*([SAW]\d+(?:\s*(?:,|;|and|&)\s*[SAW]\d+)*)\s*\]/gi,
             (_, refs) => refs.match(/[SAW]\d+/gi).map((r) => `[${r.toUpperCase()}]`).join(""))
    .replace(/\[\s+([SAW]\d+)\s*\]|\[([SAW]\d+)\s+\]/g, (_, a, b) => `[${a || b}]`)), [text]);
  return (
    <CiteContext.Provider value={onCite}>
      <div className={`md${streaming ? " streaming" : ""}`}>
        {blocks.map((b, i) => <Block key={i} b={b} tail={streaming && i === blocks.length - 1} />)}
        {streaming && blocks.length === 0 && <Cursor />}
      </div>
    </CiteContext.Provider>
  );
}
