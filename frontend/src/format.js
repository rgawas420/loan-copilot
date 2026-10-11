export const LOAN_TYPES = [
  "Home Loan", "LAP", "Working Capital", "Business Loan", "Personal Loan", "Professional Loan", "Machinery Loan",
  "New Car", "Used Car", "Commercial Vehicle", "Construction Equipment", "Education Loan", "LRD",
  "Construction Finance", "Other",
];

export const label = (key) => key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

export const money = (n) => (typeof n === "number" ? "₹" + n.toLocaleString("en-IN") : "Not computed");

export const badgeClass = (status) =>
  ({ "Strong Match": "b-strong", "Possible - Credit Check Required": "b-possible", "Not Suitable": "b-not" }[status] ||
  "b-more");

export const CHECK_MARK = { pass: "✓", fail: "✗", deviation: "!", unknown: "?", warn: "i" };
export const CHECK_LABEL = { pass: "Pass", fail: "Fails", deviation: "Deviation", unknown: "Not known", warn: "Note" };

const NUMERIC_KINDS = new Set(["money", "pin", "mobile", "age", "years", "count", "percent", "year", "cibil"]);
export const inputModeFor = (kind) => (NUMERIC_KINDS.has(kind) ? "decimal" : "text");
