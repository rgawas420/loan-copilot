"""Question-based input specification (from the Urban Money PDF)."""
import re
from dataclasses import dataclass
from typing import Callable, Optional, List, Dict, Any

@dataclass
class Q:
    key: str
    text: str
    options: Optional[List[str]] = None
    when: Optional[Callable[[Dict[str, Any]], bool]] = None

def Qs(pairs, when=None):
    """Build questions from (key, text[, options]) tuples sharing one condition."""
    out = []
    for p in pairs:
        key, text = p[0], p[1]
        opts = p[2] if len(p) > 2 else None
        out.append(Q(key, text, opts, when))
    return out

YND = ["Yes", "No", "Do Not Know"]
LOAN_TYPES = ["Home Loan", "LAP", "Working Capital", "Business Loan", "Personal Loan",
              "Professional Loan", "Machinery Loan", "New Car", "Used Car", "Commercial Vehicle",
              "Construction Equipment", "Education Loan", "LRD", "Construction Finance", "Other"]

is_sal  = lambda c: c.get("profile") == "Salaried"
is_self = lambda c: c.get("profile") in ("Self-Employed Professional", "Self-Employed Business")
lt      = lambda *names: (lambda c: c.get("loan_type") in names)

# ---------- STEP 1: common to all loans ----------
COMMON = [
    Q("loan_type", "What type of loan does the customer require?", LOAN_TYPES),
    Q("loan_amount", "How much loan does the customer require?"),
    Q("name", "What is the customer's name?"),
    Q("mobile", "What is the customer's mobile number?"),
    Q("pin", "What is the customer's current residential PIN code?"),
    Q("age", "What is the customer's age?"),
    Q("profile", "What is the customer profile?", ["Salaried", "Self-Employed Professional", "Self-Employed Business"]),
    Q("cibil", "What is the customer's CIBIL score? (number, or 'Not Checked')"),
    Q("overdue", "Is there any current overdue or DPD in any loan or credit card?", YND),
    Q("settled", "Has any loan or credit card ever been settled or written off?", YND),
    Q("existing_emi", "What is the total monthly EMI of all existing loans?"),
    Q("urgency", "How urgent is the case?", ["Immediate", "Within 7 Days", "Within 30 Days", "Normal"]),
]

# ---------- Reusable income blocks ----------
SALARIED_BLOCK = Qs([
    ("employer", "Which company/employer does the customer work for?"),
    ("designation", "What is the designation?"),
    ("gross_salary", "What is the monthly gross salary?"),
    ("net_salary", "What is the monthly net/in-hand salary?"),
    ("salary_bank", "Which bank receives the salary?"),
    ("current_job_years", "How long has the customer worked in the current company (years)?"),
    ("total_exp_years", "What is the total work experience (years)?"),
], when=is_sal)

SELF_BLOCK = Qs([
    ("business_nature", "What business does the customer operate?"),
    ("business_years", "How many years has the business been operating?"),
    ("turnover", "What is the latest annual turnover?"),
    ("itr_income", "What is the latest ITR income?"),
    ("net_profit", "What is the latest net profit?"),
    ("gst_registered", "Is the business GST registered?", ["Yes", "No"]),
], when=is_self) + [
    Q("gst_turnover_12m", "What is the last 12-month GST turnover?",
      when=lambda c: is_self(c) and c.get("gst_registered") == "Yes")]

INCOME_BLOCK = SALARIED_BLOCK + SELF_BLOCK

# ---------- Loan-specific modules ----------
HL = [
    Q("hl_purpose", "What is the purpose of the Home Loan?",
      ["New Purchase", "Resale", "Self Construction", "Plot + Construction", "Balance Transfer", "BT + Top-up"]),
    Q("prop_pin", "What is the property PIN code?"),
    Q("prop_type", "What type of property is it?", ["Flat", "Independent House", "Row House", "Plot + Construction", "Other"]),
    Q("agreement_value", "What is the agreement/purchase value of the property?"),
    Q("own_contribution", "How much own contribution will the customer bring?"),
    Q("possession", "Is the property Ready Possession or Under Construction?", ["Ready Possession", "Under Construction"]),
    Q("builder", "What is the builder/project name?"),
    Q("rera", "Is the project RERA registered?", YND),
    Q("legal_cat", "What is the property legal category?",
      ["NA", "NA-TP", "Municipal", "Gram Panchayat", "Gaothan", "Gunthewari", "R-Zone", "Sheti Na Vikas",
       "City Survey", "8A", "Patra", "Common Agreement", "Do Not Know", "Other"]),
]
HL = [Q(q.key, q.text, q.options, (lambda c: c.get("loan_type") == "Home Loan")) for q in HL] + \
     [Q(q.key, q.text, q.options, (lambda c, w=q.when: c.get("loan_type") == "Home Loan" and w(c))) for q in INCOME_BLOCK]

BT_BLOCK = lambda cond: Qs([
    ("bt_lender", "Which bank/HFC currently has the loan?"),
    ("bt_outstanding", "What is the current outstanding?"),
    ("bt_roi", "What is the current ROI (%)?"),
    ("bt_emi", "What is the current EMI?"),
    ("bt_age", "How old is the loan?"),
    ("bt_topup", "How much top-up is required (0 if none)?"),
], when=cond)
HL_BT = BT_BLOCK(lambda c: c.get("loan_type") == "Home Loan" and c.get("hl_purpose") in ("Balance Transfer", "BT + Top-up"))

PLOT = lambda cond: Qs([
    ("plot_owner", "Who owns the plot?"),
    ("plot_pin", "What is the plot PIN code?"),
    ("plot_area", "What is the plot area?"),
    ("plot_legal", "What is the property/legal category?"),
    ("plot_value", "What is the current plot value?"),
    ("constr_cost", "What is the estimated construction cost?"),
    ("approved_plan", "Is an approved building plan available?", YND),
    ("architect_est", "Is an architect estimate available?", YND),
    ("constr_stage", "What is the current construction stage?"),
], when=cond)
HL_PLOT = PLOT(lambda c: c.get("loan_type") == "Home Loan" and c.get("hl_purpose") in ("Self Construction", "Plot + Construction"))

LAP = Qs([
    ("mortgage_prop", "Which property will be mortgaged?"),
    ("prop_pin", "What is the property PIN code?"),
    ("prop_use", "Is the property Residential, Commercial or Industrial?", ["Residential", "Commercial", "Industrial"]),
    ("prop_owner", "In whose name is the property?"),
    ("owner_relation", "What is the relationship between property owner and applicant?"),
    ("market_value", "What is the approximate market value?"),
    ("prop_age", "How old is the property (years)?"),
    ("legal_cat", "What is the legal category?", ["NA", "GP", "Gaothan", "Gunthewari", "Industrial", "R-Zone", "Other", "Do Not Know"]),
    ("existing_mortgage", "Is there any existing loan/mortgage on the property?", ["Yes", "No"]),
], when=lt("LAP")) + [
    Q("mortgage_lender", "Which lender and what is the outstanding?", when=lambda c: c.get("loan_type") == "LAP" and c.get("existing_mortgage") == "Yes"),
] + Qs([("end_use", "What is the end-use of the loan?", ["Business", "Working Capital", "Purchase", "Education", "Medical", "Personal", "Other"])],
       when=lt("LAP")) + [Q(q.key, q.text, q.options, (lambda c, w=q.when: c.get("loan_type") == "LAP" and w(c))) for q in INCOME_BLOCK]

WC = Qs([
    ("biz_name", "What is the business name?"),
    ("biz_does", "What exactly does the business do?"),
    ("industry", "Which industry does the business belong to?"),
    ("constitution", "What is the constitution?", ["Proprietorship", "Partnership", "LLP", "Pvt Ltd", "Ltd"]),
    ("business_years", "How many years has the business been operating?"),
    ("biz_pin", "What is the business/factory PIN code?"),
    ("gst_registered", "Is the business GST registered?", ["Yes", "No"]),
    ("wc_limit", "How much Working Capital limit is required?"),
    ("turnover_3y", "What was the turnover in each of the last 3 financial years? (oldest to latest, separated by /)"),
    ("profit_3y", "What was the net profit in each of the last 3 financial years? (oldest to latest, separated by /)"),
    ("net_worth", "What is the latest Net Worth/Capital?"),
    ("debtors", "What are the current Debtors?"),
    ("creditors", "What are the current Creditors?"),
    ("stock", "What is the current Stock/Inventory?"),
    ("gst_turnover_12m", "What is the last 12-month GST turnover?"),
    ("gst_sales_latest", "What are the latest month GST sales?"),
    ("main_bank", "Which bank holds the main current account?"),
    ("credits_12m", "What are the total banking credits for the last 12 months?"),
    ("abb", "What is the approximate Average Bank Balance (ABB)?"),
    ("bounces_6m", "Were there cheque/EMI bounces in the last 6 months?", YND),
    ("existing_ccod", "Is there an existing CC/OD facility?", ["Yes", "No"]),
], when=lt("Working Capital")) + Qs([
    ("ccod_bank", "Which bank?"),
    ("ccod_limit", "What is the sanctioned CC/OD limit?"),
    ("ccod_util", "What is the current utilization/outstanding?"),
], when=lambda c: c.get("loan_type") == "Working Capital" and c.get("existing_ccod") == "Yes") + Qs([
    ("collateral", "Is collateral property available?", ["Yes", "No"]),
], when=lt("Working Capital")) + [
    Q("collateral_value", "What is the approximate market value of the collateral?",
      when=lambda c: c.get("loan_type") == "Working Capital" and c.get("collateral") == "Yes")]

BL = Qs([
    ("biz_does", "What does the business do?"),
    ("biz_pin", "What is the business PIN code?"),
    ("business_years", "How old is the business (years)?"),
    ("constitution", "What is the constitution?", ["Proprietorship", "Partnership", "LLP", "Pvt Ltd", "Ltd"]),
    ("turnover", "What is the latest annual turnover?"),
    ("gst_turnover_12m", "What is the last 12-month GST turnover?"),
    ("itr_turnover", "What is the latest ITR turnover?"),
    ("net_profit", "What is the latest net profit?"),
    ("main_bank", "Which bank holds the main current account?"),
    ("abb", "What is the approximate ABB?"),
    ("credits_12m", "What are the last 12-month banking credits?"),
    ("cmr", "What is the CMR? (score, or 'Do Not Know')"),
    ("bl_running", "How many existing Business Loans are running?"),
    ("unsecured_running", "How many unsecured loans are currently running?"),
    ("enquiries_3m", "How many loan enquiries were made in the last 3 months?"),
    ("bounce_overdue", "Is there any EMI/cheque bounce or overdue?", YND),
], when=lt("Business Loan"))

PL = Qs([
    ("employer", "Which company does the customer work for?"),
    ("office_pin", "What is the office PIN code?"),
    ("designation", "What is the designation?"),
    ("gross_salary", "What is the monthly gross salary?"),
    ("net_salary", "What is the monthly in-hand salary?"),
    ("salary_bank", "Which bank receives the salary?"),
    ("salary_mode", "Is salary received by Bank Transfer, Cheque or Cash?", ["Bank Transfer", "Cheque", "Cash"]),
    ("current_job_years", "How long has the customer worked in the current company (years)?"),
    ("total_exp_years", "What is the total work experience (years)?"),
    ("existing_pl", "Is there an existing Personal Loan?", ["Yes", "No"]),
], when=lt("Personal Loan")) + [
    Q("pl_outstanding", "What is the outstanding on the existing Personal Loan?",
      when=lambda c: c.get("loan_type") == "Personal Loan" and c.get("existing_pl") == "Yes")]

PROF = Qs([
    ("profession", "What is the profession?", ["Doctor", "CA", "CS", "Architect", "Consultant", "Other"]),
    ("qualification", "What is the qualification?"),
    ("specialization", "What is the specialization?"),
    ("registration", "Is professional registration available?", YND),
    ("practice_years", "How many years has the customer been practicing?"),
    ("owns_premises", "Does the customer own the clinic/office?", YND),
    ("gross_receipts", "What are the annual gross receipts?"),
    ("itr_income", "What is the ITR income?"),
    ("credits_annual", "What are the annual banking credits?"),
], when=lt("Professional Loan"))

MACH = Qs([
    ("machine", "Which machine is being purchased?"),
    ("machine_cond", "Is the machine New or Used?", ["New", "Used"]),
    ("brand", "Who is the manufacturer/brand?"),
    ("model", "What is the machine model?"),
    ("supplier", "Who is the supplier?"),
    ("supplier_loc", "Where is the supplier located?"),
    ("basic_cost", "What is the basic machine cost?"),
    ("invoice_amt", "What is the invoice amount including GST?"),
    ("margin", "How much margin will the customer contribute?"),
    ("install_pin", "Where will the machine be installed? (PIN code)"),
    ("machine_use", "What will the machine be used for?"),
    ("business_years", "How old is the business (years)?"),
    ("turnover", "What is the annual turnover?"),
    ("net_profit", "What is the net profit?"),
    ("cmr", "What is the CMR? (score, or 'Do Not Know')"),
], when=lt("Machinery Loan"))

NEWCAR_FIELDS = [
    ("car_make", "What is the car make?"),
    ("car_model", "What is the model/variant?"),
    ("dealer", "What is the dealer name and location?"),
    ("exshowroom", "What is the ex-showroom price?"),
    ("onroad", "What is the on-road price?"),
    ("down_payment", "How much down payment will the customer make?"),
    ("income_amt", "What is the customer's monthly income?"),
    ("rto", "At which RTO will the car be registered?"),
]
NEWCAR = Qs(NEWCAR_FIELDS, when=lt("New Car", "Used Car"))
USEDCAR = Qs([
    ("mfg_year", "What is the manufacturing year?"),
    ("reg_year", "What is the registration year?"),
    ("owner_no", "Which ownership number is current (1st/2nd/3rd etc.)?"),
    ("rc_name", "In whose name is the RC?"),
    ("seller_type", "Is the seller an Individual or Dealer?", ["Individual", "Dealer"]),
    ("valuation", "What is the current vehicle valuation?"),
    ("existing_hypo", "Is there existing finance/hypothecation?", ["Yes", "No"]),
], when=lt("Used Car")) + [
    Q("hypo_outstanding", "What is the outstanding?", when=lambda c: c.get("loan_type") == "Used Car" and c.get("existing_hypo") == "Yes"),
    Q("km_run", "Approximately how many kilometres has the car run?", when=lt("Used Car"))]

CV = Qs([
    ("veh_cond", "Is the vehicle New or Used?", ["New", "Used"]),
    ("veh_type", "What is the vehicle type?", ["LCV", "HCV", "Truck", "Bus", "Tipper", "Other"]),
    ("veh_model", "What are the make and model?"),
    ("veh_cost", "What is the vehicle cost?"),
    ("dealer_loc", "Where is the dealer located?"),
    ("rto", "What is the RTO location?"),
    ("buyer_type", "Is the customer a First-Time Buyer or Fleet Owner?", ["First-Time Buyer", "Fleet Owner"]),
], when=lt("Commercial Vehicle")) + Qs([
    ("fleet_total", "How many total vehicles are owned?"),
    ("fleet_financed", "How many vehicles are financed?"),
    ("fleet_debtfree", "How many vehicles are debt-free?"),
    ("fleet_financiers", "Which financiers fund the existing vehicles?"),
    ("repay_track", "What is the repayment track?"),
], when=lambda c: c.get("loan_type") == "Commercial Vehicle" and c.get("buyer_type") == "Fleet Owner") + Qs([
    ("veh_use", "What will the vehicle be used for?"),
    ("biz_income", "What is the monthly business income?"),
], when=lt("Commercial Vehicle"))

CE = Qs([
    ("equipment", "Which equipment is required?", ["Excavator", "JCB", "Crane", "Loader", "Other"]),
    ("equip_cond", "Is it New or Used?", ["New", "Used"]),
    ("manufacturer", "Who is the manufacturer?"),
    ("model", "What is the model?"),
    ("supplier", "Who is the supplier?"),
    ("equip_cost", "What is the equipment cost?"),
    ("customer_contribution", "What is the customer contribution?"),
    ("units_owned", "How many equipment units does the customer currently own?"),
    ("work_orders", "Are Work Orders/Contracts available?", YND),
    ("business_years", "What is the business vintage (years)?"),
    ("turnover", "What is the turnover?"),
    ("cmr", "What is the CMR? (score, or 'Do Not Know')"),
], when=lt("Construction Equipment"))

EDU = Qs([
    ("student_age", "What is the student age?"),
    ("course", "Which course will the student pursue?"),
    ("institute", "Which university/institute?"),
    ("country", "In which country?"),
    ("admission", "Is admission confirmed?", YND),
    ("course_duration", "What is the course duration?"),
    ("prev_qualification", "What is the previous qualification?"),
    ("academic_score", "What is the academic percentage/CGPA?"),
    ("course_cost", "What is the total course cost?"),
    ("tuition", "What are the tuition fees?"),
    ("living_exp", "What are the estimated living expenses?"),
    ("edu_secured", "Is a Secured or Unsecured loan required?", ["Secured", "Unsecured"]),
    ("coapplicant", "Who is the co-applicant?"),
    ("coapp_income", "What is the co-applicant's annual income?"),
    ("coapp_cibil", "What is the co-applicant CIBIL score?"),
], when=lt("Education Loan")) + [
    Q("edu_collateral_value", "What is the collateral property value?",
      when=lambda c: c.get("loan_type") == "Education Loan" and c.get("edu_secured") == "Secured")]

LRD = Qs([
    ("prop_pin", "Where is the property? (PIN code)"),
    ("prop_use", "Is the property Commercial or Residential?", ["Commercial", "Residential"]),
    ("market_value", "What is the property market value?"),
    ("prop_owner", "In whose name is the property?"),
    ("tenant", "Who is the tenant?"),
    ("tenant_cat", "What is the tenant category?", ["Bank", "MNC", "Corporate", "Retail Brand", "Government", "Other"]),
    ("monthly_rent", "What is the monthly rent?"),
    ("lease_start", "When did the lease start?"),
    ("lease_expiry", "What is the lease expiry date?"),
    ("lockin_expiry", "When does the lock-in period expire?"),
    ("rent_escalation", "What is the rent escalation percentage?"),
    ("rent_bank", "Which bank receives the rent?"),
    ("existing_loan", "Is there an existing loan on the property?", ["Yes", "No"]),
], when=lt("LRD"))

CIP = Qs([
    ("cip_property", "Which property is being purchased?"),
    ("prop_use", "Is it Commercial or Industrial?", ["Commercial", "Industrial"]),
    ("prop_pin", "What is the property PIN code?"),
    ("midc", "Is it located in MIDC/Industrial Zone?", YND),
    ("land_building", "Is it Land Only or Land + Building?", ["Land Only", "Land + Building"]),
    ("purchase_price", "What is the purchase price?"),
    ("market_value", "What is the market value?"),
    ("own_business", "Will the property be used for the customer's own business?", ["Yes", "No"]),
    ("business_years", "What is the business vintage (years)?"),
    ("turnover", "What is the annual turnover?"),
    ("net_profit", "What is the net profit?"),
    ("gst_turnover_12m", "What is the GST turnover?"),
    ("cmr", "What is the CMR? (score, or 'Do Not Know')"),
], when=lambda c: c.get("loan_type") == "Other" and c.get("other_kind") == "Commercial/Industrial Purchase")

CF = Qs([
    ("developer", "What is the developer/builder name?"),
    ("dev_years", "How many years has the developer been in business?"),
    ("proj_completed", "How many projects have been completed?"),
    ("proj_ongoing", "How many projects are ongoing?"),
    ("project_name", "What is the project name?"),
    ("project_pin", "What is the project PIN code?"),
    ("rera", "Is the project RERA registered?", YND),
    ("land_owned", "Is the land owned by the developer?", YND),
    ("project_cost", "What is the total project cost?"),
    ("promoter_contribution", "How much promoter contribution has been invested?"),
    ("pct_complete", "What percentage of construction is complete?"),
    ("units_total", "How many total units are there?"),
    ("units_sold", "How many units are sold?"),
    ("collection", "How much collection has been received?"),
    ("receivables", "What are the pending receivables?"),
    ("existing_proj_loan", "Is there an existing project loan?", ["Yes", "No"]),
], when=lt("Construction Finance")) + [
    Q("proj_loan_detail", "Who is the lender and what is the outstanding?",
      when=lambda c: c.get("loan_type") == "Construction Finance" and c.get("existing_proj_loan") == "Yes")]

# Standalone "Balance Transfer + Top-up" module (used when loan_type == 'Other' & other_kind == BT)
BT_STANDALONE = Qs([
    ("bt_loan_kind", "Which loan is to be transferred?", ["HL", "LAP", "PL", "BL", "WC"]),
    ("bt_lender", "Who is the current lender?"),
    ("bt_orig_amount", "What was the original sanctioned amount?"),
    ("bt_sanction_date", "When was the loan sanctioned?"),
    ("bt_outstanding", "What is the current outstanding?"),
    ("bt_emi", "What is the current EMI?"),
    ("bt_roi", "What is the current ROI (%)?"),
    ("bt_tenure_left", "What is the remaining tenure (months)?"),
    ("bt_bounce_12m", "Was there any EMI bounce in the last 12 months?", YND),
    ("bt_topup", "How much top-up is required (0 if none)?"),
    ("bt_topup_purpose", "What is the purpose of the top-up?"),
], when=lambda c: c.get("loan_type") == "Other" and c.get("other_kind") == "Balance Transfer + Top-up")

OTHER_KIND = [Q("other_kind", "What kind of 'Other' loan is this?",
                ["Commercial/Industrial Purchase", "Balance Transfer + Top-up", "Something else"],
                when=lt("Other"))]

# Where two modules share a key, the first applicable one wins (see applicable())
ALL_QUESTIONS = (COMMON + OTHER_KIND + HL + HL_BT + HL_PLOT + LAP + WC + BL + PL + PROF + MACH + NEWCAR +
                 USEDCAR + CV + CE + EDU + LRD + CIP + CF + BT_STANDALONE)
# Salaried/self-employed income questions for Professional / Education etc. are covered by their own modules.

# ---------------------------------------------------------------------------
# Personal data: these answers identify a person or business. They are kept in the saved case
# but NEVER sent to the LLM, web search or audit log (see llm_safe_case).
# ---------------------------------------------------------------------------
PERSONAL_KEYS = {"name", "mobile", "prop_owner", "plot_owner", "rc_name", "coapplicant", "tenant", "employer",
                 "biz_name", "builder", "developer", "project_name", "dealer", "supplier", "supplier_loc",
                 "mortgage_prop", "cip_property", "owner_relation"}
KNOWN_KEYS = {q.key for q in ALL_QUESTIONS}


def llm_safe_case(case):
    """Allowlist: only known questionnaire keys that are not personal data, and only answered ones."""
    return {k: v for k, v in case.items()
            if k in KNOWN_KEYS and k not in PERSONAL_KEYS and v not in (None, "")}


# ---------------------------------------------------------------------------
# Answer types and validation
# ---------------------------------------------------------------------------
_MONEY = re.compile(r"amount|emi|salary|income|cost|value|price|turnover|profit|outstanding|limit|worth|debtors|"
                    r"creditors|stock|credits|abb|receipts|rent$|contribution|margin|tuition|living_exp|collection|"
                    r"receivables|topup$|onroad|exshowroom|down_payment|invoice|sales")
_YEARS = {"business_years", "practice_years", "dev_years", "prop_age", "current_job_years", "total_exp_years"}
_COUNT = {"bl_running", "unsecured_running", "enquiries_3m", "fleet_total", "fleet_financed", "fleet_debtfree",
          "units_owned", "units_total", "units_sold", "proj_completed", "proj_ongoing", "bt_tenure_left", "km_run"}
_PERCENT = {"bt_roi", "pct_complete", "rent_escalation"}
_NOT_CHECKED = re.compile(r"(?i)not\s*checked|na|n/?a|nh|no\s*history|-1")


def kind_of(q: Q) -> str:
    k = q.key
    if q.options: return "choice"
    if k == "mobile": return "mobile"
    if k.endswith("pin"): return "pin"
    if k in ("cibil", "coapp_cibil"): return "cibil"
    if k in ("age", "student_age"): return "age"
    if k in ("mfg_year", "reg_year"): return "year"
    if k in _YEARS: return "years"
    if k in _COUNT: return "count"
    if k in _PERCENT: return "percent"
    if k in ("turnover_3y", "profit_3y"): return "money_list"
    if _MONEY.search(k): return "money"
    return "text"


PLACEHOLDER = {"money": "e.g. 12.5 lakh or 45,000", "money_list": "e.g. 40 lakh / 45 lakh / 52 lakh",
               "pin": "6-digit PIN code", "mobile": "10-digit mobile number", "cibil": "300-900, or Not Checked",
               "age": "Age in years", "years": "e.g. 3 or 2.5", "count": "A whole number", "percent": "e.g. 8.5",
               "year": "e.g. 2021", "text": ""}


def validate(q: Q, value):
    """Return (normalised value, error message or None)."""
    from app.rag.eligibility import num, num_list
    v = str(value if value is not None else "").strip()
    kind = kind_of(q)
    if not v:
        return None, "Enter an answer, or skip this question."
    if len(v) > 300:
        return None, "Keep the answer under 300 characters."
    if kind == "choice":
        match = next((o for o in q.options if o.lower() == v.lower()), None)
        return (match, None) if match else (None, "Choose one of the options.")
    if kind == "mobile":
        d = re.sub(r"\D", "", v)
        d = d[2:] if len(d) == 12 and d.startswith("91") else d
        return (d, None) if re.fullmatch(r"[6-9]\d{9}", d) else (None, "Enter a valid 10-digit Indian mobile number.")
    if kind == "pin":
        return (v, None) if re.fullmatch(r"[1-9]\d{5}", v) else (None, "PIN code must be 6 digits.")
    if kind == "cibil":
        if _NOT_CHECKED.fullmatch(v):
            return "Not Checked", None
        n = num(v)
        if n is not None and 300 <= n <= 900:
            return str(int(n)), None
        return None, "CIBIL must be between 300 and 900, or 'Not Checked'."
    n = num(v)
    if kind == "age":
        lo, hi = (15, 60) if q.key == "student_age" else (18, 80)
        return (str(int(n)), None) if n is not None and lo <= n <= hi else (None, f"Age must be between {lo} and {hi}.")
    if kind == "year":
        return (str(int(n)), None) if n is not None and 1980 <= n <= 2100 else (None, "Enter a 4-digit year.")
    if kind == "years":
        return (v, None) if n is not None and 0 <= n <= 80 else (None, "Enter the number of years, e.g. 3 or 2.5.")
    if kind == "count":
        return (str(int(n)), None) if n is not None and n >= 0 and n == int(n) else (None, "Enter a whole number.")
    if kind == "percent":
        return (v, None) if n is not None and 0 <= n <= 100 else (None, "Enter a percentage between 0 and 100.")
    if kind == "money":
        if n is None or n < 0 or n > 1e11:
            return None, "Enter an amount, e.g. 12.5 lakh, 1.2 cr or 45,000."
        if q.key == "loan_amount" and n <= 0:
            return None, "Loan amount must be more than zero."
        return v, None
    if kind == "money_list":
        return (v, None) if num_list(v) else (None, "Enter amounts separated by /, e.g. 40 lakh / 45 lakh.")
    return v, None


# ---------------------------------------------------------------------------
# Readiness gate: minimum critical information per loan type
# ---------------------------------------------------------------------------
MIN_COMMON = ["loan_type", "loan_amount", "age", "profile", "cibil", "overdue", "settled", "existing_emi", "pin"]

MIN_BY_LOAN = {
    "Home Loan": ["hl_purpose", "agreement_value", "prop_pin", "possession", "legal_cat"],
    "LAP": ["market_value", "prop_use", "prop_pin", "legal_cat"],
    "Working Capital": ["wc_limit", "business_years", "turnover_3y"],
    "Business Loan": ["business_years", "turnover", "net_profit"],
    "Personal Loan": ["net_salary", "employer", "current_job_years", "salary_mode"],
    "Professional Loan": ["profession", "practice_years", "itr_income"],
    "Machinery Loan": ["machine", "invoice_amt", "business_years"],
    "New Car": ["onroad", "income_amt"],
    "Used Car": ["onroad", "valuation", "income_amt"],
    "Commercial Vehicle": ["veh_cost", "buyer_type", "biz_income"],
    "Construction Equipment": ["equip_cost", "business_years"],
    "Education Loan": ["course", "country", "course_cost", "coapp_income"],
    "LRD": ["market_value", "monthly_rent", "tenant_cat"],
    "Construction Finance": ["project_cost", "rera", "pct_complete"],
}


def critical_keys(case):
    need = list(MIN_COMMON) + MIN_BY_LOAN.get(case.get("loan_type"), [])
    if case.get("loan_type") in ("Home Loan", "LAP") and case.get("profile"):
        need += ["net_salary"] if case.get("profile") == "Salaried" else ["itr_income"]
    return need


def missing_critical(case):
    return [k for k in critical_keys(case) if case.get(k) in (None, "")]


def enough_info(case):
    m = missing_critical(case)
    if not m:
        return True, "We have enough information to find suitable lenders."
    return False, "More information required: " + ", ".join(label_of(k) for k in m)


# ---------------------------------------------------------------------------
# Question flow
# ---------------------------------------------------------------------------
def applicable(case):
    seen, out = set(), []
    for q in ALL_QUESTIONS:
        if q.key in seen:
            continue
        if q.when is None or q.when(case):
            seen.add(q.key); out.append(q)
    return out


def _answered(case, key):
    return case.get(key) not in (None, "")


def skipped(case):
    return set(case.get("_skipped") or [])


def next_question(case):
    """Critical questions first (so the partner reaches 'ready' quickly), then the optional ones.
    Skipped questions are not asked again."""
    qs = [q for q in applicable(case) if not _answered(case, q.key) and q.key not in skipped(case)]
    crit = set(critical_keys(case))
    return next((q for q in qs if q.key in crit), None) or (qs[0] if qs else None)


def find_question(case, key):
    return next((q for q in applicable(case) if q.key == key), None)


def progress(case):
    qs = applicable(case)
    done = sum(1 for q in qs if _answered(case, q.key) or q.key in skipped(case))
    return done, len(qs)


def label_of(key):
    return key.replace("_", " ").capitalize()


def prune_case(case):
    """Drop stale answers that no longer apply (e.g. user changed loan type) and unknown keys."""
    keys = {q.key for q in applicable(case)}
    out = {k: v for k, v in case.items() if k in keys and v not in (None, "")}
    sk = sorted(k for k in skipped(case) if k in keys and k not in out)
    if sk:
        out["_skipped"] = sk
    return out


def apply_answer(case, key, value=None, skip=False):
    """Validate one answer and return (new case, error). Critical questions cannot be skipped."""
    case = dict(case)
    q = find_question(case, key)
    if q is None:
        return case, "That question does not apply to this case."
    if skip:
        if key in critical_keys(case):
            return case, "This question is needed to match lenders, so it cannot be skipped."
        case.pop(key, None)
        case["_skipped"] = sorted(skipped(case) | {key})
        return prune_case(case), None
    norm, err = validate(q, value)
    if err:
        return case, err
    case[key] = norm
    case["_skipped"] = sorted(skipped(case) - {key})
    return prune_case(case), None


def answered_summary(case):
    out = []
    for q in applicable(case):
        if _answered(case, q.key):
            out.append({"key": q.key, "label": label_of(q.key), "value": str(case[q.key]), "skipped": False})
        elif q.key in skipped(case):
            out.append({"key": q.key, "label": label_of(q.key), "value": "Skipped", "skipped": True})
    return out


def question_payload(q, case):
    if q is None:
        return None
    kind = kind_of(q)
    return {"key": q.key, "text": q.text, "options": q.options, "kind": kind,
            "placeholder": PLACEHOLDER.get(kind, ""), "required": q.key in critical_keys(case)}


def next_payload(case):
    q = next_question(case)
    done, total = progress(case)
    ready, msg = enough_info(case)
    return {
        "question": question_payload(q, case),
        "answered": done, "total": total,
        "ready": ready, "message": msg,
        "missing": [] if ready else missing_critical(case),
        "summary": answered_summary(case),
    }


def field_catalogue():
    """Compact list of every question for free-text intake extraction (personal fields excluded)."""
    seen, out = set(), []
    for q in ALL_QUESTIONS:
        if q.key in seen or q.key in PERSONAL_KEYS:
            continue
        seen.add(q.key)
        out.append(f"{q.key}: {q.text}" + (f" Options: {', '.join(q.options)}" if q.options else ""))
    return "\n".join(out)
