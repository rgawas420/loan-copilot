"""Banker matching for customer profiles: filtering, de-duplication, conflicts, ranking and the table."""
from app.rag import bankers

AUG = "03 - Banker & RM Lists/Pan India RM List Aug 2026.xlsx"
JUN = "03 - Banker & RM Lists/Pan India RM list Jun-26..xlsx"
OLD = "03 - Banker & RM Lists/Rm List.xlsx"
METAS = {AUG: {"title": "Pan India RM List Aug 2026"}, JUN: {"title": "Pan India RM list Jun-26"}, OLD: {"title": "Rm List"}}


def row(lender, name, phone, city="Pune", loan="Home Loan"):
    parts = [f"Lender Name: {lender}"] + ([f"Type of Loan: {loan}"] if loan else []) + [f"City*: {city}",
             f"Bank/ NBFC RM Name: {name}", f"Bank/ NBFC RM Contact No: {phone}"]
    return " | ".join(parts)


def rank(rows, loan_type="Home Loan", city="Pune"):
    return bankers.rank(bankers.Match(loan_type=loan_type, city=city), rows, METAS, {})


def test_parses_labelled_columns_only():
    assert bankers._fields(row("Axis Bank", "Ajit", "8983240885")) == {
        "lender": "Axis Bank", "name": "Ajit", "phone": "8983240885", "city": "Pune", "loan_type": "Home Loan"}
    assert bankers._fields("Column B: Shree Balaji | Column C: 9035146481") == {}


def test_list_month_comes_from_the_effective_date_or_the_name():
    assert bankers._recency(AUG, "Pan India RM List Aug 2026", None) == 202608
    assert bankers._recency("x", "Pan India RM list Augest 2026", None) == 202608
    assert bankers._recency("x", "Pan India RM list Jun-26", None) == 202606
    assert bankers._recency("x", "Rm List", "2026-01-01") == 202601
    assert bankers._recency("x", "Rm List", None) == 0


def test_other_cities_products_and_unlabelled_rows_are_left_out():
    m = rank([(AUG, row("Axis Bank", "Ajit", "8983240885")),
              (AUG, row("HDFC Bank", "Nilesh", "9881993383", city="Mumbai")),
              (AUG, row("Kotak", "Mangesh", "9168237864", loan="Personal Loan")),
              (AUG, "Column B: Shree Balaji | Column C: 9035146481 | Pune | Home Loan")])
    assert [b.lender for b in m.top] == ["Axis Bank"]


def test_duplicates_merge_and_the_latest_list_wins():
    m = rank([(OLD, row("Axis Bank", "Ajit", "+91 89832 40885")), (JUN, row("Axis Bank", "Ajit", "8983240885")),
              (AUG, row("Axis Bank", "Ajit", "8983240885"))])
    assert len(m.top) == 1 and m.top[0].path == AUG and m.top[0].lists == 3 and m.duplicates == 2
    assert m.top[0].notes == []                                  # +91 and spaces are the same number


def test_a_newer_number_replaces_an_old_one_but_equal_lists_are_flagged():
    newer = rank([(JUN, row("Axis Bank", "Ajit", "9000000001")), (AUG, row("Axis Bank", "Ajit", "9000000002"))])
    assert newer.top[0].phone == "9000000002" and newer.top[0].notes == [] and newer.conflicts == 0
    tie = rank([(AUG, row("Axis Bank", "Ajit", "9000000001")), (AUG, row("Axis Bank", "Ajit", "9000000002"))])
    assert tie.conflicts == 1 and tie.top[0].notes[0].startswith("verify: also listed as")


def test_strong_matches_first_one_banker_per_lender_then_also_check():
    rows = [(AUG, row(f"Lender {i:02d}", "RM A", f"90000000{i:02d}")) for i in range(12)]
    rows += [(AUG, row("Lender 00", "RM B", "9111111111"))]                    # second RM of a lender
    rows += [(AUG, row("General Bank", "RM C", "9222222222", loan=""))]        # no loan type named: Good
    m = rank(rows)
    assert len(m.top) == 10 and len({b.lender for b in m.top}) == 10
    assert all(b.match == "Strong" for b in m.top)
    assert "General Bank" in m.also and "Lender 00" not in m.also
    assert m.top[0].lender == "Lender 00"                                     # most RMs in the city first


def test_bad_numbers_are_marked_not_changed():
    m = rank([(AUG, row("Axis Asha", "Chhagan", "86684892371"))])
    assert m.top[0].phone == "86684892371" and m.top[0].notes == ["verify number"]


def test_table_and_no_match_message():
    m = rank([(AUG, row("Axis Bank", "Ajit", "8983240885"))])
    bankers.number(m, [{"ref": "S1", "source": AUG}])
    assert m.table().splitlines()[2] == "| 1 | Axis Bank | Ajit [S1] | 8983240885 | Strong |"
    assert rank([]).table() == bankers.NOT_FOUND
    assert "No matching banker was found in the knowledge base" in bankers.NOT_FOUND
