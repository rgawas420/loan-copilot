"""Banker directory (normalised RM lists built by the sync), the contact check on answers, newest-first table
lookups and the eligibility input fixes."""
from contextlib import closing
from pathlib import Path

import pytest

import app.rag.vectorstore as vectorstore
from app.core.config import get_settings
from app.rag import bankers, grounding, lookup
from app.rag.eligibility import indicative_eligibility
from app.rag.profile import compute
from app.services import banker_directory as bd
from app.services import knowledge_base as kb

HEAD = ["Lender Name", "Type of Loan", "City", "Bank/ NBFC RM Name", "Bank/ NBFC RM Contact No"]


class FakeStore:
    def __init__(self):
        self.docs = {}

    def add_documents(self, docs, ids=None):
        self.docs.update(zip(ids, docs))

    def delete(self, ids=None):
        for i in ids:
            self.docs.pop(i, None)


@pytest.fixture
def folder(monkeypatch, tmp_path):
    root = tmp_path / "Urban Money"
    (root / "03 - Banker & RM Lists").mkdir(parents=True)
    monkeypatch.setattr(get_settings(), "urban_money_dir", root)
    monkeypatch.setattr(get_settings(), "kb_index_db", tmp_path / "kb.db")
    monkeypatch.setattr(get_settings(), "kb_cache_dir", tmp_path / "cache")
    monkeypatch.setattr(vectorstore, "is_pinecone", lambda: True)
    store = FakeStore()
    monkeypatch.setattr(vectorstore, "get_vectorstore", lambda: store)
    from app.services import kb_versions
    kb_versions._invalidate()
    bd._cities.clear()
    bd._city_names.clear()
    return root


def _xlsx(path: Path, rows):
    import openpyxl
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    for r in [HEAD] + rows:
        wb.active.append(r)
    wb.save(path)


JUN = [["HDFC LTD", "Home Loan", "Pune", "MR Ajit Pawar", "9800000001"],
       ["ICICI BANK", "Top Up-HL", "Pune", "Neha Joshi - 9800000002", ""],
       ["Axis Bank", "Personal Loan", "Pune", "Karan Mehra", "9800000003"]]
AUG = [["HDFC Bank", "Home Loan", "Pune", "Ajit Pawar", "9800000001"],
       ["HDFC Credila Financial Services Private Limited", "Education Loan", "Pune", "Rohit Panchal", "9800000004"],
       ["Kotak", "HLUC", "BENGALURU", "Mangesh G", "98000000055"]]


# ------------------------------------------------------------------ normalisation
def test_lender_names_map_to_a_known_bank_only_when_nothing_else_remains():
    assert bd.canonical_lender("HDFC LTD") == ("HDFC Bank", True)
    assert bd.canonical_lender("ICICI BANK") == ("ICICI Bank", True)
    assert bd.canonical_lender("BAJAJ") == ("Bajaj Finserv", True)
    assert bd.canonical_lender("TATA CAPITAL HOUSING FINANCE LIMITED") == ("Tata Capital", True)
    assert bd.canonical_lender("HDFC Credila Financial Services Private Limited")[1] is False
    assert bd.canonical_lender("Muthoot Fincorp")[1] is False          # not Muthoot Finance


def test_products_cities_names_and_phones_are_normalised():
    assert [bd.canonical_product(p) for p in ("Top Up-HL", "BT-HL", "HLUC", "Affordable - HL", "Auto Loan", "")] == \
           ["Home Loan", "Home Loan", "Home Loan", "Home Loan", "Car Loan", ""]
    assert bd.canonical_product("Insurance") == "Insurance"               # unknown: kept, matches only itself
    assert bd.canonical_city("BENGALURU") == "Bangalore" and bd.canonical_city("pune ") == "Pune"
    assert bd.clean_name("MR Jagdish Mali - 9352562316") == ("Jagdish Mali", "9352562316")
    assert bd.clean_name("KAPIL (8923241902)")[1] == "8923241902"
    assert bd.digits("+91 98000 00001") == "9800000001" and bd.valid_mobile("09800000001")
    assert not bd.valid_mobile("98000000055") and not bd.valid_mobile("5800000001")


# ------------------------------------------------------------------ build + query
def test_sync_builds_a_deduplicated_directory_newest_list_first(folder):
    lists = folder / "03 - Banker & RM Lists"
    _xlsx(lists / "Pan India RM list Jun-26.xlsx", JUN)
    _xlsx(lists / "Pan India RM list Aug 2026.xlsx", AUG)
    _xlsx(folder / "WhatsApp Chat" / "Pan India RM list Aug 2026.xlsx", AUG)       # byte-identical copy
    sync = kb.sync()
    assert sync["duplicates"] == ["WhatsApp Chat/Pan India RM list Aug 2026.xlsx = "
                                  "03 - Banker & RM Lists/Pan India RM list Aug 2026.xlsx"]
    report = sync["banker_directory"]
    assert report["files"] == 2 and "duplicate_file" not in report["issues"]       # the copy was never indexed
    assert report["issues"]["phone_in_name"] == 1 and report["issues"]["invalid_phone"] == 1

    m = bankers.find("Home Loan", "Pune")
    assert m.source == "directory"
    ajit = next(b for b in m.top if b.name == "Ajit Pawar")
    assert ajit.lender == "HDFC Bank" and ajit.recency == 202608 and ajit.lists == 2   # HDFC LTD + HDFC Bank merged
    assert ajit.path.startswith("03 - Banker")                                        # never the copy
    neha = next(b for b in m.top if b.name == "Neha Joshi")
    assert neha.phone == "9800000002" and neha.lender == "ICICI Bank" and neha.match == "Strong"
    assert "Karan Mehra" not in {b.name for b in m.top}                               # another product
    assert m.total == 2

    assert [b.name for b in bankers.find("Home Loan", "Pune", ["ICICI Bank"]).top] == ["Neha Joshi"]
    blr = bankers.find("Home Loan", "Bengaluru")
    assert [b.notes for b in blr.top] == [["verify number"]]

    q = bd.quality_report()
    assert q["current"] and q["issues"]["invalid_phone"]["count"] == 1


def test_an_out_of_date_directory_is_never_used(folder):
    _xlsx(folder / "03 - Banker & RM Lists" / "Rm List Aug 2026.xlsx", AUG)
    kb.sync()
    with closing(kb._db()) as db:
        db.execute("UPDATE dir_meta SET value='stale' WHERE key='fingerprint'")
        db.commit()
    m = bankers.find("Home Loan", "Pune")
    assert m.source == "row index" and [b.name for b in m.top] == ["Ajit Pawar"]
    assert bd.known_city("hdfc home loan rm in pune") == ""                   # no directory: no banker route
    bd.rebuild()
    assert bankers.find("Home Loan", "Pune").source == "directory"
    assert bd.known_city("hdfc home loan rm in pune") == "Pune"


def test_table_lookup_counts_identical_files_once_and_shows_the_newest_list_first(folder):
    lists = folder / "03 - Banker & RM Lists"
    _xlsx(lists / "Pan India RM list Jun-26.xlsx", JUN * 3)                       # more matching rows, older
    _xlsx(lists / "Pan India RM list Aug 2026.xlsx", AUG)
    _xlsx(folder / "WhatsApp Chat" / "Pan India RM list Aug 2026.xlsx", AUG)
    kb.sync()
    look = lookup.find("hdfc rm list pune", ["HDFC Bank"], [])
    assert [f["path"] for f in look.files] == ["03 - Banker & RM Lists/Pan India RM list Aug 2026.xlsx",
                                               "03 - Banker & RM Lists/Pan India RM list Jun-26.xlsx"]
    assert look.file_count == 2


# ------------------------------------------------------------------ contact check
CONTEXT = """[S1] Pan India RM list Aug 2026 | file: x.xlsx
Lender Name: HDFC Bank | Banker / RM: Ajit Pawar | Contact No: 9800000001
Lender Name: ICICI Bank | Banker / RM: Neha Joshi | Contact No: 9800000002
SL NO: 1 | RAH HEAD NAME: Abhishek Khare | RAH HEAD CONTACT: 919003587162 | HO-DM: Satya Saurabh | CONTACT NO: 918651488414"""


def test_a_number_moved_to_another_banker_is_caught():
    good = "| 1 | HDFC Bank | Ajit Pawar [S1] | 9800000001 |\n| 2 | ICICI Bank | Neha Joshi [S1] | 9800000002 |"
    assert grounding.verify_contacts(good, CONTEXT) == []
    swapped = "| 1 | HDFC Bank | Ajit Pawar [S1] | 9800000002 |\n| 2 | ICICI Bank | Neha Joshi [S1] | 9800000001 |"
    assert grounding.verify_contacts(swapped, CONTEXT) == [
        "9800000002 (listed for a different person in the sources)",
        "9800000001 (listed for a different person in the sources)"]
    # several people on one source line: only judged against the whole line, so no false alarm
    assert grounding.verify_contacts("Satya Saurabh: 918651488414", CONTEXT) == []
    assert grounding.verify_contacts("Call 9800000009", CONTEXT) == []       # not in the sources: figure check
    assert grounding.verify_contacts(grounding.NOT_FOUND, CONTEXT) == []


# ------------------------------------------------------------------ eligibility inputs
def test_a_yearly_income_in_the_monthly_field_is_converted():
    r = compute({"loan_type": "Home Loan", "monthly_income": "18 LPA", "loan_amount": "60 lakh"})
    assert r["income"] == 150000
    assert compute({"loan_type": "Home Loan", "monthly_income": "₹1,50,000"})["income"] == 150000


def test_workflow_says_when_existing_emis_were_not_deducted():
    base = {"loan_type": "Home Loan", "profile": "Salaried", "net_salary": "150000", "loan_amount": "60 lakh"}
    e = indicative_eligibility(base)
    assert e["existing_emi_known"] is False and e["note"].startswith("Existing EMIs were not captured")
    e = indicative_eligibility({**base, "existing_emi": "0"})
    assert e["existing_emi_known"] is True and not e["note"].startswith("Existing EMIs")


def test_contact_files_with_other_layouts_are_read():
    bajaj = ("Location: Pune | SM: Govind Shelke | Contact No.: 72765 | 30385 | ASM: D-Ashutosh Jaiswal | "
             "Contact No.: 73553 | 41865")
    assert bd.people_in_row(bajaj, "Bajaj Finserv") == [
        {"lender": "Bajaj Finserv", "name": "Govind Shelke", "phone": "7276530385", "city": "Pune", "designation": "SM"},
        {"lender": "Bajaj Finserv", "name": "D-Ashutosh Jaiswal", "phone": "7355341865", "city": "Pune", "designation": "ASM"}]
    canara = ("SL NO: 1 | CIRCLE OFFICE: AHMEDABAD | REGIONAL OFFICE: GANDHINAGAR | RAH HEAD NAME: Abhishek Khare | "
              "RAH HEAD CONTACT: 919003587162 | RAH HEAD EMAIL: a@canarabank.com | CO DM/AGM: M R Kulkarni | "
              "DESIGNATION: AGM | CO-DM/AGM CONTACT: 919822726939")
    people = bd.people_in_row(canara, "Canara Bank")
    assert [(p["name"], p["phone"], p["city"], p["designation"]) for p in people] == [
        ("Abhishek Khare", "919003587162", "GANDHINAGAR", "RAH HEAD NAME"), ("M R Kulkarni", "919822726939", "GANDHINAGAR", "AGM")]
    listed = "SR NO.: 2 | COMPANY NAME: INCREAD | NAME: SANDEEP MOURYA (MUMBAI) | CONTACT NUMBER: 7208658905"
    assert bd.people_in_row(listed) == [{"lender": "INCREAD", "name": "SANDEEP MOURYA", "phone": "7208658905",
                                         "city": "MUMBAI", "designation": "NAME"}]
    assert bd.people_in_row("Contact No.: First 5 | Last 5 | RSM Contact No: First 5 | Last 5", "Bajaj Finserv") == []


def test_a_code_question_reads_the_code_book_not_the_rm_lists(folder):
    _xlsx(folder / "03 - Banker & RM Lists" / "Pan India RM list Aug 2026.xlsx",
          [["Kotak", "Machinery Loan", "Pune", f"RM {i}", f"98000001{i:02d}"] for i in range(5)])
    import openpyxl
    wb = openpyxl.Workbook()
    for r in [["BANK NAME", "PRODUCT", "CODE"], ["KOTAK MAHINDRA", "MACHINERY LOAN", "171504070"]]:
        wb.active.append(r)
    wb.save(folder / "AUG 26 - CODES AND CONTACTS.xlsx")
    kb.sync()
    look = lookup.find("Kotak machinery loan code", ["Kotak Mahindra Bank"], ["Machinery Loan"])
    assert [f["path"] for f in look.files] == ["AUG 26 - CODES AND CONTACTS.xlsx"]
    rm = lookup.find("Kotak machinery loan rm list pune", ["Kotak Mahindra Bank"], ["Machinery Loan"])
    assert rm.files[0]["path"].startswith("03 - Banker")                     # no type preference for RMs
