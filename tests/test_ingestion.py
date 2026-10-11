from pathlib import Path

from app.core.config import BASE_DIR
from app.services.ingestion import load_and_chunk, load_directory


def test_sample_kb_sections_keep_metadata():
    chunks = load_directory(BASE_DIR / "tests" / "fixtures" / "sample_kb")
    assert chunks
    assert all(c.metadata["lender"].startswith("Sample Lender") for c in chunks)
    assert {"Home Loan", "Personal Loan", "LAP", "Business Loan"} <= {c.metadata["loan_type"] for c in chunks}
    assert any("UM-SAMPLE-A01" in c.page_content for c in chunks)


def test_plain_text_is_chunked(tmp_path: Path):
    f = tmp_path / "p.txt"; f.write_text("Policy line. " * 400)
    chunks = load_and_chunk(f)
    assert len(chunks) > 1 and chunks[0].metadata["loan_type"] == "Unknown"


def test_long_markdown_section_keeps_contact_line_in_every_chunk(tmp_path: Path):
    body = "\n".join(f"Rule {i}: applicants must meet condition number {i} of the policy." for i in range(60))
    f = tmp_path / "long.md"
    f.write_text(f"# Lender: Big Bank\n\n## Program: HL Prime\nLoan type: Home Loan\n{body}\n"
                 "Banker/RM: R One | Mobile: 90000-12345 | Urban Money Code: UM-BB-1 | Policy last verified: 2026-09-01\n")
    chunks = load_and_chunk(f)
    assert len(chunks) > 1
    assert all("UM-BB-1" in c.page_content and c.page_content.startswith("Lender: Big Bank") for c in chunks)
    assert all(c.metadata["loan_type"] == "Home Loan" for c in chunks)


def test_uploader_tags_apply_to_untagged_documents(tmp_path: Path):
    f = tmp_path / "p.txt"; f.write_text("LAP policy text")
    c = load_and_chunk(f, lender="Real Bank", loan_type="LAP")[0]
    assert c.metadata["lender"] == "Real Bank" and c.metadata["loan_type"] == "LAP"
    assert c.page_content.startswith("Lender: Real Bank")


def test_percentage_cells_are_indexed_as_shown_in_excel(tmp_path):
    import openpyxl
    from app.services.ingestion import extract
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Bank Name", "Home Loan payout", "Code"])
    ws.append(["Aadhar Housing", 0.0115, 26903])
    ws["B2"].number_format = "0.00%"
    wb.save(tmp_path / "slab.xlsx")
    assert extract(tmp_path / "slab.xlsx")[0].rows == ["Bank Name: Aadhar Housing | Home Loan payout: 1.15% | Code: 26903"]
