"""Domain vocabulary for query understanding and document tagging: banks / lenders, loan products,
acronyms and the financial facts a question can ask for (each with the evidence an answer needs).

Matching is deterministic (word-boundary regexes), so the same question always gives the same entities.
Admin-entered bank / product names from the document registry are added to the built-in lists at runtime.
"""
import re
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Tuple

# canonical name -> aliases (lower case). Longer aliases are tried first, so "pnb housing" wins over "pnb".
BANKS: Dict[str, List[str]] = {
    "HDFC Bank": ["hdfc bank", "hdfc"],
    "ICICI Bank": ["icici bank", "icici"],
    "State Bank of India": ["state bank of india", "sbi"],
    "Axis Bank": ["axis bank", "axis"],
    "Axis Finance": ["axis finance"],
    "Kotak Mahindra Bank": ["kotak mahindra", "kotak"],
    "Bajaj Finserv": ["bajaj finserv", "bajaj finance", "bajaj housing finance", "bajaj"],
    "Aditya Birla Capital": ["aditya birla", "abfl", "abhfl", "abcl"],
    "Cholamandalam (Chola)": ["cholamandalam", "chola"],
    "Bandhan Bank": ["bandhan bank", "bandhan"],
    "Canara Bank": ["canara bank", "canara"],
    "Central Bank of India": ["central bank of india", "cent bank"],
    "Piramal Finance": ["piramal"],
    "Poonawalla Fincorp": ["poonawalla fincorp", "poonawalla"],
    "IDFC First Bank": ["idfc first", "idfc"],
    "IndusInd Bank": ["indusind"],
    "Fibe": ["fibe"],
    "Finnable": ["finnable"],
    "Indifi": ["indifi"],
    "Protium": ["protium"],
    "Utkarsh Small Finance Bank": ["utkarsh"],
    "Tata Capital": ["tata capital"],
    "L&T Finance": ["l&t finance", "l & t finance", "ltfs", "l&t"],
    "PNB Housing Finance": ["pnb housing", "pnbhfl"],
    "Punjab National Bank": ["punjab national bank", "pnb"],
    "LIC Housing Finance": ["lic housing", "lic hfl", "lichfl"],
    "Yes Bank": ["yes bank"],
    "Federal Bank": ["federal bank"],
    "South Indian Bank": ["south indian bank"],
    "City Union Bank": ["city union bank", "city union"],
    "Standard Chartered": ["standard chartered", "stanchart", "scb"],
    "SMFG India Credit": ["smfg", "fullerton"],
    "JM Financial": ["jm financial"],
    "Sitara Housing Finance": ["sitara"],
    "Ratnaafin": ["ratnaafin"],
    "Rupeek": ["rupeek"],
    "IIFL Finance": ["iifl"],
    "Hero FinCorp": ["hero fincorp"],
    "Godrej Capital": ["godrej capital", "godrej housing"],
    "Shriram Finance": ["shriram"],
    "Mahindra Finance": ["mahindra finance"],
    "Muthoot Finance": ["muthoot"],
    "AU Small Finance Bank": ["au small finance", "au sfb", "au bank"],
    "Ujjivan Small Finance Bank": ["ujjivan"],
    "Equitas Small Finance Bank": ["equitas"],
    "Bank of Baroda": ["bank of baroda"],
    "Union Bank of India": ["union bank of india", "union bank"],
    "Aavas Financiers": ["aavas"],
    "Home First Finance": ["home first", "homefirst"],
    "UGRO Capital": ["ugro"],
    "Lendingkart": ["lendingkart"],
    "NeoGrowth": ["neogrowth"],
}

# canonical product -> aliases. Names follow the questionnaire's loan types where one exists.
PRODUCTS: Dict[str, List[str]] = {
    "Home Loan": ["home loan", "housing loan", "home finance", "hl"],
    "LAP": ["loan against property", "mortgage loan", "property loan", "lap"],
    "Business Loan": ["unsecured business loan", "business loan", "msme loan", "sme loan", "ubl", "bl"],
    "Personal Loan": ["personal loan", "pl"],
    "Professional Loan": ["professional loan", "doctor loan", "doctors loan", "ca loan"],
    "Working Capital": ["working capital", "cash credit", "overdraft", "cc limit", "od limit"],
    "Machinery Loan": ["machinery loan", "machine loan", "equipment finance", "equipment loan"],
    "Car Loan": ["new car loan", "used car loan", "car loan", "auto loan"],
    "Commercial Vehicle": ["commercial vehicle", "cv loan"],
    "Construction Equipment": ["construction equipment"],
    "Education Loan": ["education loan"],
    "LRD": ["lease rental discounting", "lrd"],
    "Construction Finance": ["construction finance"],
    "Gold Loan": ["gold loan"],
    "Balance Transfer": ["balance transfer"],
}

# Acronyms partners use; both forms are searched so keyword search matches either spelling.
ACRONYMS: Dict[str, str] = {
    "hl": "home loan", "lap": "loan against property", "bl": "business loan", "pl": "personal loan",
    "ubl": "unsecured business loan", "roi": "rate of interest", "foir": "fixed obligation to income ratio",
    "ltv": "loan to value", "pf": "processing fee", "cv": "commercial vehicle", "lrd": "lease rental discounting",
    "dsa": "direct selling agent", "rm": "relationship manager", "sm": "sales manager", "emi": "monthly instalment",
    "itr": "income tax return", "gst": "goods and services tax", "cibil": "credit score", "bt": "balance transfer",
    "abb": "average bank balance", "dpd": "days past due", "ntb": "new to bank", "stp": "straight through processing",
}

# A fact a question can ask for. trigger: when the question asks for it. search: words added to the search
# query. evidence: words a passage must contain to answer it. value: the kind of value the answer needs
# (None = no specific value pattern, e.g. a document list).
METRICS: Dict[str, dict] = {
    "max_amount": {"trigger": r"\b(max(imum)?|highest|biggest|largest)\b.{0,40}\b(amount|loan|limit|ticket|funding|quantum)\b|how much (can|could|will|would)|\bup ?to\b|ticket size|loan limit",
                   "search": "maximum loan amount", "evidence": r"max|maximum|up ?to|upto|ticket|limit|loan amount|quantum|amount", "value": "amount"},
    "min_amount": {"trigger": r"\b(min(imum)?|lowest|smallest)\b.{0,40}\b(loan amount|amount|ticket)\b",
                   "search": "minimum loan amount", "evidence": r"min|minimum|ticket|loan amount|amount", "value": "amount"},
    "roi": {"trigger": r"\broi\b|rate of interest|interest rate|\binterest\b|\brates?\b",
            "search": "rate of interest ROI", "evidence": r"\broi\b|interest|\brates?\b", "value": "percent"},
    "processing_fee": {"trigger": r"processing (fee|charge)|\bpf\b|login fee|charges",
                       "search": "processing fee charges", "evidence": r"processing|fee|charge|\bpf\b", "value": "percent_or_amount"},
    "tenure": {"trigger": r"tenure|tenor|repayment (period|term)|how long|loan term|\bterm\b",
               "search": "tenure repayment period", "evidence": r"tenure|tenor|term|repayment|years?|months?", "value": "duration"},
    "cibil": {"trigger": r"cibil|credit score|bureau score|\bscore\b",
              "search": "CIBIL score credit score", "evidence": r"cibil|score|bureau|credit", "value": "score"},
    "foir": {"trigger": r"\bfoir\b|obligation",
             "search": "FOIR fixed obligation to income ratio", "evidence": r"foir|obligation", "value": "percent"},
    "ltv": {"trigger": r"\bltv\b|loan to value|funding (percentage|%)|margin",
            "search": "LTV loan to value", "evidence": r"\bltv\b|loan to value|funding|margin", "value": "percent"},
    "min_income": {"trigger": r"(min(imum)?\.?\s+)?(income|salary|turnover)\s*(criteria|requirement|required|needed|eligibility)?|earn",
                   "search": "minimum income salary turnover requirement", "evidence": r"income|salary|turnover|earning", "value": "amount"},
    "age": {"trigger": r"\bage\b|how old",
            "search": "age criteria", "evidence": r"\bage\b|years|yrs", "value": "duration"},
    "vintage": {"trigger": r"vintage|business (stability|continuity|years)|years in business|work experience|experience",
                "search": "business vintage stability", "evidence": r"vintage|stability|continuity|experience|years", "value": "duration"},
    "contact": {"trigger": r"contact|phone|mobile|\bnumber of\b|\brm\b|\bsm\b|banker|manager|whom|email|reach",
                "search": "RM contact mobile number", "evidence": r"\d{10}|\+91|@|mobile|phone|contact|name", "value": None},
    "code": {"trigger": r"\bcodes?\b|dsa code|connector code|product code|loan code",
             "search": "code", "evidence": r"code", "value": "code"},
    "pincode": {"trigger": r"pin ?codes?|serviceable|serviceability|\b\d{6}\b",
                "search": "pincode serviceable", "evidence": r"pin|\b\d{6}\b", "value": "pincode"},
    "payout": {"trigger": r"payout|commission|brokerage|\bgrid\b|slab",
               "search": "payout percentage commission", "evidence": r"payout|commission|brokerage|slab|grid|%", "value": "percent"},
    "documents": {"trigger": r"documents?|kyc|paperwork|checklist",
                  "search": "documents required checklist", "evidence": r"document|kyc|itr|statement|proof|pan|aadhaar", "value": None},
    "eligibility": {"trigger": r"eligib|criteria|qualify|norms|policy",
                    "search": "eligibility criteria norms", "evidence": r"eligib|criteria|norms|minimum|maximum|policy", "value": None},
}

# The value an answer needs, as a regex over the passage.
VALUE_PATTERNS = {
    "amount": r"₹|\brs\.?\s*\d|\binr\b|\blakhs?\b|\blacs?\b|\bcrores?\b|\bcr\b|\d+(\.\d+)?\s*(l|k|cr)\b|\d{1,3}(,\d{2,3}){1,}|\b\d{5,}\b",
    "percent": r"\d+(\.\d+)?\s*%|percent|\bbps\b",
    "percent_or_amount": r"\d+(\.\d+)?\s*%|percent|₹|\brs\.?\s*\d|\d{1,3}(,\d{2,3}){1,}",
    "duration": r"\d+\s*(years?|yrs?|months?|mths?|y\b|m\b)",
    "score": r"\b[3-9]\d{2}\b",
    "code": r"\b(?=[A-Za-z0-9-]*\d)[A-Za-z0-9-]{3,}\b",
    "pincode": r"\b\d{6}\b",
}

FOLLOWUP_START = re.compile(r"^\s*(and|what about|how about|same|also|what if|then|for|in|now|ok|okay|and for)\b", re.I)
PRONOUNS = re.compile(r"\b(it|its|that|this|they|them|those|these|same|there|above|previous|earlier)\b", re.I)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().replace("_", " ").replace("-", " ")).strip()


@lru_cache(maxsize=4096)
def _alias_re(alias: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9&])" + re.escape(alias).replace(r"\ ", r"\s+") + r"(?![a-z0-9&])")


def _find(text: str, table: Dict[str, List[str]]) -> List[str]:
    """Canonical names whose aliases occur in text, in order of first appearance; a longer alias claims its span."""
    t = _norm(text)
    taken: List[Tuple[int, int]] = []
    hits: List[Tuple[int, str]] = []
    pairs = sorted(((a, c) for c, aliases in table.items() for a in aliases), key=lambda x: -len(x[0]))
    for alias, canon in pairs:
        for m in _alias_re(alias).finditer(t):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            taken.append((m.start(), m.end()))
            hits.append((m.start(), canon))
    out: List[str] = []
    for _, c in sorted(hits):
        if c not in out:
            out.append(c)
    return out


def _with_extra(table: Dict[str, List[str]], extra: Iterable[str]) -> Dict[str, List[str]]:
    """Add names entered by admins (document metadata) that the built-in table does not know."""
    known = {a for aliases in table.values() for a in aliases} | {c.lower() for c in table}
    merged = dict(table)
    for name in extra:
        n = _norm(name)
        if n and len(n) >= 3 and n not in known:
            merged[name.strip()] = [n]
    return merged


# Aliases added by admins (app/services/aliases.py), on top of the built-in tables: canonical -> [aliases].
_ADMIN: Dict[str, Dict[str, List[str]]] = {"bank": {}, "product": {}}
_merged: Dict[str, Dict[str, List[str]]] = {}


def set_admin_aliases(kind: str, table: Dict[str, List[str]]):
    _ADMIN[kind] = {c: [_norm(a) for a in al if _norm(a)] for c, al in table.items()}
    _merged.pop(kind, None)


def table(kind: str) -> Dict[str, List[str]]:
    """The built-in bank / product table plus the admins' aliases (a new canonical name is added as one)."""
    if kind not in _merged:
        merged = {c: list(a) for c, a in (BANKS if kind == "bank" else PRODUCTS).items()}
        for canon, aliases in _ADMIN[kind].items():
            have = merged.setdefault(canon, [_norm(canon)])
            have.extend(a for a in aliases if a not in have)
        _merged[kind] = merged
    return _merged[kind]


def find_banks(text: str, extra: Iterable[str] = ()) -> List[str]:
    return _find(text, _with_extra(table("bank"), extra))


def find_products(text: str, extra: Iterable[str] = ()) -> List[str]:
    return _find(text, _with_extra(table("product"), extra))


def find_metrics(text: str) -> List[str]:
    t = _norm(text)
    out = [m for m, d in METRICS.items() if re.search(d["trigger"], t)]
    if "max_amount" in out and "min_income" in out and not re.search(r"income|salary|turnover|earn", t):
        out.remove("min_income")
    if "min_amount" in out and "max_amount" in out and not re.search(r"max", t):
        out.remove("max_amount")
    # "maximum tenure / age / LTV for a home loan" asks for that fact, not the loan amount
    others = {"tenure", "age", "ltv", "foir", "roi", "cibil", "vintage", "processing_fee", "payout"}
    if "max_amount" in out and others & set(out) and not re.search(r"amount|how much|ticket|limit|quantum|funding", t):
        out.remove("max_amount")
    return out


def bank_aliases(bank: str) -> List[str]:
    return table("bank").get(bank, [_norm(bank)])


def product_aliases(product: str) -> List[str]:
    return table("product").get(product, [_norm(product)])


def mentions(text: str, names: Iterable[str], table: Dict[str, List[str]]) -> bool:
    t = _norm(text)
    for n in names:
        for a in table.get(n, [_norm(n)]):
            if _alias_re(a).search(t):
                return True
    return False


def expand_acronyms(text: str) -> List[str]:
    """Long forms of the acronyms in text (HL -> home loan)."""
    words = set(re.findall(r"[a-z]+", _norm(text)))
    return [v for k, v in ACRONYMS.items() if k in words]


def salient_terms(text: str) -> List[str]:
    """Numbers / codes and quoted phrases: these must survive any query rewrite unchanged."""
    terms = re.findall(r'"([^"]{2,60})"', text)
    terms += [t for t in re.findall(r"[A-Za-z0-9]+", text) if any(ch.isdigit() for ch in t)]
    return list(dict.fromkeys(terms))


def is_followup(question: str, history: Optional[list]) -> bool:
    if not history:
        return False
    words = re.findall(r"\w+", question)
    return len(words) <= 6 or bool(FOLLOWUP_START.search(question)) or bool(PRONOUNS.search(question))


def infer_category(name: str, products: List[str]) -> str:
    n = _norm(name)
    for pat, cat in ((r"payout|slab|grid|capping|clawback", "Payout"), (r"pin ?code", "Pincode list"),
                     (r"code book|codes?\b", "Codes"), (r"\brm\b|contact|banker|\bsm\b|\.vcf$", "Contacts"),
                     (r"company list|employer|supplier", "Company list"), (r"policy|norms|reckoner|variants", "Policy"),
                     (r"poster|brochure|one pager|flyer|presentation", "Product brochure")):
        if re.search(pat, n):
            return cat
    return products[0] if products else "General"


# Document type from the file name (first match wins), then from the text.
_DOC_TYPE_NAME = [(r"payout|slab|grid|capping|clawback", "Payout"), (r"pin ?code", "Pincode list"),
                  (r"code book|codes?\b", "Codes"), (r"\brm\b|contact|banker|\bsm\b|\.vcf$", "Contacts"),
                  (r"company list|employer|supplier", "Company list"), (r"rate (card|sheet)|\broi\b|interest rate", "Rate sheet"),
                  (r"\bfaqs?\b", "FAQ"), (r"policy|norms|reckoner|variants|eligibility", "Policy"),
                  (r"poster|brochure|one pager|flyer|presentation|deck", "Product brochure")]
_DOC_TYPE_TEXT = [("Payout", r"payout|brokerage|commission"), ("Rate sheet", r"rate of interest|\broi\b|interest rate"),
                  ("Eligibility", r"eligib|\bfoir\b|\bltv\b|cibil|minimum (income|salary|age)|vintage"),
                  ("Policy", r"\bpolicy\b|\bnorms\b|deviation|guideline"), ("FAQ", r"\bfaqs?\b|frequently asked"),
                  ("Product brochure", r"apply now|call now|\boffer\b|contact us|whatsapp|limited period")]


def infer_doc_type(name: str, sample: str = "") -> str:
    """One of the knowledge base's document types (never a product, unlike infer_category): from the file name,
    else from the start of the text (lists of phone numbers or pincodes, then the most frequent type wording,
    when it occurs at least twice); else General."""
    n = _norm(name)
    for pat, t in _DOC_TYPE_NAME:
        if re.search(pat, n):
            return t
    text = (sample or "")[:3000]
    if len(re.findall(r"(?<!\d)[6-9]\d{9}(?!\d)", text)) >= 5:
        return "Contacts"
    if len(re.findall(r"(?<!\d)[1-8]\d{5}(?!\d)", text)) >= 20:
        return "Pincode list"
    low = text.lower()
    scored = sorted(((len(re.findall(pat, low)), t) for t, pat in _DOC_TYPE_TEXT), reverse=True)
    return scored[0][1] if scored and scored[0][0] >= 2 else "General"
