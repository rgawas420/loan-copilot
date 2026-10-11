"""Recommendation-aware follow-ups and answer states: "other than above bank" excludes every lender already shown,
"no more lenders" is not "not found", what-ifs never change the recorded customer, a new case carries nothing,
"why not X?" comes from rule results only, and failures are never reported as missing information."""
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

import app.rag.bankers as bankers
import app.rag.chat as chat
import app.rag.followup as followup
import app.rag.lender_eligibility as le
import app.rag.llm as llm
import app.rag.profile as profile
import app.rag.retrieval as retrieval
import app.rag.states as states
from app.services import recommendations

AUG = "03 - Banker & RM Lists/Pan India RM List Aug 2026.xlsx"
ASKED = "Salaried customer, ₹45,000 a month, CIBIL 780, no EMIs, home loan in Pune: which bankers?"
FIELDS = {"loan_type": "Home Loan", "employment": "Salaried", "monthly_income": "₹45,000", "cibil": "780",
          "existing_emi": "None", "property_location": "Pune"}
ROWS = {"Axis Bank": ("Ajit", "8983240885"), "HDFC Bank": ("Atul", "9822000001"), "Tata Capital": ("Gaurav", "9822000002")}


def _rows(lenders):
    return [(AUG, f"Lender Name: {l} | Type of Loan: Home Loan | City*: Pune | Bank/ NBFC RM Name: {ROWS[l][0]} | "
                  f"Bank/ NBFC RM Contact No: {ROWS[l][1]}") for l in lenders]


def _find(available):
    """bankers.find over these lenders' rows, honouring lenders / exclude like the real one."""
    def find(lt, city, lenders=None, exclude=None):
        res = bankers.Match(loan_type=lt, city=city, lenders_filter=list(lenders or []), excluded=list(exclude or []))
        return bankers.rank(res, _rows(available), {AUG: {"title": "Pan India RM List Aug 2026"}}, {})
    return find


def _customer(**kw):
    p = profile.Profile(question=ASKED, fields={**FIELDS, **kw.pop("fields", {})}, **kw)
    p.calc = profile.compute(p.fields)
    return p


@pytest.fixture
def no_search(monkeypatch):
    monkeypatch.setattr(retrieval, "vector_hits", lambda q, k, lt, banks=None: [])
    monkeypatch.setattr(retrieval, "keyword_hits", lambda q, k, exact: [])


def _answer_for(p, match):
    """A profile answer as the model writes it (the tables copied)."""
    also = "\n".join(f"- {a}" for a in match.also)
    return (f"Summary.\n\n### Customer Profile\n{p.profile_table()}\n\n### Bankers / Lenders to Approach — Pune\n"
            f"{match.table()}\n\n### Also Check\n{also}\n")


# ------------------------------------------------------------------ alternative lenders
def test_other_lenders_exclude_every_lender_already_shown(monkeypatch, no_search):
    monkeypatch.setattr(bankers, "find", _find(["Axis Bank", "HDFC Bank", "Tata Capital"]))
    prep = chat.Prepared("kb", "other than above bank", {}, {}, None, [], [], "", use_llm=False,
                         profile=_customer(exclude=["Axis Bank", "HDFC Bank"], relation="alternative_lenders"))
    assert prep.bankers.lenders == ["Tata Capital"]                          # no duplicates of what was shown
    prompt = "\n".join(m.content for m in prep.messages)
    assert "ALREADY SHOWN" in prompt and "Axis Bank, HDFC Bank" in prompt
    assert states.decide(prep, prep.finish("Tata Capital: Gaurav 9822000002 [S1]"), None)["state"] == \
        states.ALTERNATIVE_RESULTS_FOUND


def test_no_more_lenders_is_its_own_state_not_a_knowledge_gap(monkeypatch, no_search):
    monkeypatch.setattr(bankers, "find", _find(["Axis Bank"]))
    prep = chat.Prepared("kb", "any other option", {}, {}, None, [], [], "", use_llm=False,
                         profile=_customer(exclude=["Axis Bank"], relation="alternative_lenders"))
    assert prep.state == states.NO_ADDITIONAL_MATCHES and not prep.not_found
    assert "beyond the 1 already listed" in prep.direct
    info = states.decide(prep, prep.finish(prep.direct), None)
    assert info["state"] == states.NO_ADDITIONAL_MATCHES and not info["retry"]
    labels = {a["label"] for a in info["actions"]}
    assert labels >= {"Change city", "Change loan type", "New case"}
    assert "Change loan amount" in labels                 # the sample Home Loan rules use the amount
    # without lender rules for the loan type: said plainly, and the amount is not offered (it changes nothing)
    prep = chat.Prepared("kb", "any other option", {}, {}, None, [], [], "", use_llm=False,
                         profile=_customer(fields={"loan_type": "Education Loan"}, exclude=["Axis Bank"]))
    assert "not a judgement on eligibility" in prep.direct
    assert "Change loan amount" not in {a["label"] for a in states.decide(prep, prep.finish(prep.direct), None)["actions"]}


def test_unreadable_rm_lists_are_a_failure_not_an_empty_result(monkeypatch, no_search):
    def broken(lt, city, lenders=None, exclude=None):
        return bankers.Match(loan_type=lt, city=city, excluded=list(exclude or []), error="RM lists: OperationalError")
    monkeypatch.setattr(bankers, "find", broken)
    prep = chat.Prepared("kb", "other than above bank", {}, {}, None, [], [], "", use_llm=False,
                         profile=_customer(exclude=["Axis Bank"], relation="alternative_lenders"))
    assert prep.state == states.RETRIEVAL_FAILURE and "couldn't read the RM lists" in prep.direct
    assert states.decide(prep, prep.finish(prep.direct), None)["retry"]


def test_bankers_find_excludes_by_canonical_lender_name(monkeypatch):
    m = bankers.rank(bankers.Match(loan_type="Home Loan", city="Pune", excluded=["hdfc"]),
                     _rows(["Axis Bank", "HDFC Bank"]), {AUG: {"title": "Aug"}}, {})
    assert m.lenders == ["Axis Bank"]


# ------------------------------------------------------------------ resolving the previous recommendation
def _history(p, match):
    return [{"role": "user", "text": ASKED}, {"role": "assistant", "text": _answer_for(p, match)}]


def test_the_stored_recommendation_is_used_only_for_the_answer_that_showed_it():
    p = _customer()
    match = _find(["Axis Bank", "HDFC Bank"])("Home Loan", "Pune")
    hist = _history(p, match)
    record = {"id": "r1", "hypothetical": False, "profile": {"fields": dict(FIELDS)}, "seen": ["Tata Capital"],
              "question": ASKED}
    a = followup.active(hist, record)
    assert a.record_id == "r1" and a.seen == ["Tata Capital", "Axis Bank", "HDFC Bank"]   # cumulative
    other = {**record, "profile": {"fields": {**FIELDS, "cibil": "650"}}}                  # another customer
    b = followup.active(hist, other)
    assert b.record_id is None and b.seen == ["Axis Bank", "HDFC Bank"]


def _decided(monkeypatch, relation, lenders=(), clarification="", options=()):
    monkeypatch.setattr(llm, "get_llm", lambda kind="fast", max_tokens=None: object())
    monkeypatch.setattr(followup, "_known_banks", lambda: [])
    monkeypatch.setattr(followup, "_decide", lambda m, q, h, a: followup._Decision(
        relation=relation, lenders=list(lenders), clarification=clarification, options=list(options)))


def test_other_than_above_excludes_the_lenders_of_the_previous_answer(monkeypatch):
    _decided(monkeypatch, "alternative_lenders")
    match = _find(["Axis Bank", "HDFC Bank"])("Home Loan", "Pune")
    r = followup.resolve("other than above bank", _history(_customer(), match))
    assert r.relation == "alternative_lenders" and r.profile.exclude == ["Axis Bank", "HDFC Bank"]
    assert r.profile.fields == FIELDS                                          # same customer


def test_their_contact_numbers_resolve_to_the_lenders_named_without_rms(monkeypatch):
    _decided(monkeypatch, "contact_followup")
    match = bankers.Match(loan_type="Home Loan", city="Pune")
    match = bankers.rank(match, _rows(["Axis Bank"]), {AUG: {"title": "Aug"}}, {})
    match.also = ["Tata Capital", "HDFC Bank"]
    r = followup.resolve("give me their contact numbers", _history(_customer(), match))
    assert r.relation == "contact_followup" and r.lenders == ["Tata Capital", "HDFC Bank"]
    assert r.profile.lenders == ["Tata Capital", "HDFC Bank"]


def test_a_what_if_is_labelled_and_leaves_the_customer_unchanged(monkeypatch):
    _decided(monkeypatch, "what_if")
    monkeypatch.setattr(profile, "extract", lambda q: profile.Profile(question=q, fields={"monthly_income": "₹50,000"}))
    match = _find(["Axis Bank"])("Home Loan", "Pune")
    hist = _history(_customer(), match)
    r = followup.resolve("what if salary is 50000?", hist)
    assert r.relation == "what_if" and r.profile.hypothetical
    assert r.profile.fields["monthly_income"] == "₹50,000" and "₹45,000 → ₹50,000" in r.profile.scenario
    assert r.active.fields["monthly_income"] == "₹45,000"                      # the recorded customer is unchanged
    sections = dict(r.profile.sections())
    assert any("Hypothetical scenario" in k for k in sections)


def test_a_loan_type_change_needs_a_new_loan_type(monkeypatch):
    _decided(monkeypatch, "loan_type_change")
    match = _find(["Axis Bank"])("Home Loan", "Pune")
    monkeypatch.setattr(profile, "extract", lambda q: profile.Profile(question=q, fields={"loan_type": "Business Loan"}))
    r = followup.resolve("what about business loan?", _history(_customer(), match))
    assert r.relation == "loan_type_change" and r.profile.loan_type == "Business Loan"
    assert r.profile.seen_before == []                                          # a new search, nothing excluded
    monkeypatch.setattr(profile, "extract", lambda q: profile.Profile(question=q, fields={"cibil": "700"}))
    assert followup.resolve("make it business", _history(_customer(), match)).relation == "followup"


def test_a_new_case_carries_nothing_over(monkeypatch):
    _decided(monkeypatch, "new_case")
    match = _find(["Axis Bank"])("Home Loan", "Pune")
    route, history = chat._route("kb", "start a new case", _history(_customer(), match), [], "")
    assert route.direct == followup.NEW_CASE_REPLY and history == []
    assert followup.active([{"role": "user", "text": "start a new case"},
                            {"role": "assistant", "text": followup.NEW_CASE_REPLY}]) is None


def test_an_ambiguous_message_gets_one_question_with_options(monkeypatch):
    _decided(monkeypatch, "clarify", clarification="Which lender and loan type?",
             options=["HDFC home loan ROI", "Axis LAP ROI", "Provide your standard market ROI range"])
    hist = [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "Hello! How can I help?"}]
    r = followup.resolve("what is the ROI?", hist)
    assert r.direct == "Which lender and loan type?"
    assert r.options == ["HDFC home loan ROI", "Axis LAP ROI"]          # an option naming no lender / product: dropped
    _decided(monkeypatch, "clarify")                                            # no question written: not asked
    assert followup.resolve("what is the ROI?", hist).relation == "followup"


def test_direct_route_replies_have_their_state(monkeypatch):
    _decided(monkeypatch, "clarify", clarification="Which lender?", options=["HDFC", "Axis"])
    monkeypatch.setattr(chat, "_llm", lambda required=True: object())
    hist = [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "Hello!"}]
    out = chat.run("kb", "what is the ROI?", history=hist)
    assert out["answer"] == "Which lender?" and out["state"]["state"] == states.CLARIFICATION_REQUIRED
    assert [a["text"] for a in out["state"]["actions"]] == ["HDFC", "Axis"]


# ------------------------------------------------------------------ eligibility from the rules engine only
def test_lender_outcomes_come_from_loaded_rules_only():
    salaried = {**FIELDS, "age": "35", "loan_amount": "₹30,00,000"}
    assert le.evaluate_lender(salaried, "HDFC Bank")["outcome"] == le.NO_POLICY        # no rules: no verdict
    r = le.evaluate_lender({**salaried, "cibil": "650"}, "Sample Lender A")
    assert r["outcome"] == le.NOT_ELIGIBLE and any("650" in f for f in r["failed"])
    r = le.evaluate_lender(salaried, "Sample Lender A")
    assert r["outcome"] == le.MISSING and any("job" in m.lower() for m in r["missing"])  # years in job not given
    assert "can't verify" in le.explain(le.evaluate_lender(salaried, "HDFC Bank"), salaried)
    said = le.explain(le.evaluate_lender(salaried, "HDFC Bank"), salaried).lower()
    assert "not eligible" not in said and "rejected" not in said and "does not fit" not in said


def test_why_not_a_lender_states_rules_and_list_position_only(monkeypatch, no_search):
    monkeypatch.setattr(bankers, "find", _find(["HDFC Bank"]))
    p = _customer(lenders=["HDFC Bank"], explain=["HDFC Bank"], shown_before=["Axis Bank"], relation="explain_lender")
    prep = chat.Prepared("kb", "why not HDFC?", {}, {}, None, [], [], "", use_llm=False, profile=p)
    why = dict(p.sections(prep.bankers))["WHY HDFC BANK (established by the app; copy exactly)"]
    assert "can't verify" in why and "has 1 Home Loan RM(s) for Pune" in why and "not in the top 10" in why


# ------------------------------------------------------------------ failures are never "not found"
@pytest.mark.parametrize("err, state", [
    (Exception("Rate limit reached (429)"), states.RATE_LIMITED),
    (TimeoutError("read timed out"), states.TEMPORARY_SERVICE_FAILURE),
    (sqlite3.OperationalError("database is locked"), states.TEMPORARY_SERVICE_FAILURE),
    (type("APIConnectionError", (Exception,), {})("boom"), states.TEMPORARY_SERVICE_FAILURE),
    (KeyError("x"), states.INTERNAL_ERROR),
])
def test_errors_map_to_failure_states(err, state):
    assert states.for_error(err) == state


def test_a_failed_search_is_reported_as_a_failure_with_retry(monkeypatch):
    from app.rag import vectorstore

    def timing_out(q, k, lt, banks=None):
        vectorstore.note_issue("timeout")
        return []
    monkeypatch.setattr(retrieval, "vector_hits", timing_out)
    monkeypatch.setattr(retrieval, "keyword_hits", lambda q, k, exact: [])
    prep = chat.Prepared("kb", "What is the HDFC business loan processing fee?", {}, {}, None, [], [], "", use_llm=False)
    assert not prep.not_found and prep.state == states.RETRIEVAL_FAILURE
    assert prep.direct == states.RETRIEVAL_FAILURE_REPLY
    info = states.decide(prep, prep.finish(prep.direct), None)
    assert info["retry"] and info["notice"]


def test_nothing_found_by_a_working_search_is_a_knowledge_gap(no_search):
    prep = chat.Prepared("kb", "What is the HDFC business loan processing fee?", {}, {}, None, [], [], "", use_llm=False)
    assert prep.not_found and prep.state is None
    info = states.decide(prep, prep.finish(chat.NOTHING_FOUND), None)
    assert info["state"] == states.KNOWLEDGE_BASE_GAP and not info["retry"] and info["actions"] == []


def test_stale_and_partially_verified_answers(no_search):
    prep = chat.Prepared("kb", "q", {}, {}, None, [], [], "", use_llm=False)
    prep.refs = [{"ref": "S1", "older_edition": True}]
    post = {"not_found": False, "sources": [{"ref": "S1"}], "unsupported_figures": [], "contact_mismatches": []}
    assert states.decide(prep, post, None)["state"] == states.STALE_SOURCE
    prep.refs = [{"ref": "S1", "older_edition": False}]
    assert states.decide(prep, post, {"level": "high"})["state"] == states.ANSWER_VERIFIED
    assert states.decide(prep, post, {"level": "medium"})["state"] == states.ANSWER_PARTIALLY_VERIFIED
    assert states.decide(prep, {**post, "unsupported_figures": ["₹5 crore"]}, None)["state"] == \
        states.ANSWER_PARTIALLY_VERIFIED


# ------------------------------------------------------------------ isolation
def test_recommendations_belong_to_one_user_and_conversation():
    conv = uuid.uuid4().hex
    rid = recommendations.save(1, conv, relation="new_profile", question=ASKED, loan_type="Home Loan", city="Pune",
                               profile={"fields": FIELDS}, shown=["Axis Bank"], seen=["Axis Bank"], evaluations=[],
                               sources=[AUG], state=states.ANSWER_VERIFIED)
    assert recommendations.latest(1, conv)["id"] == rid
    assert recommendations.latest(2, conv) is None and recommendations.get(rid, 2) is None   # another partner
    assert recommendations.latest(1, uuid.uuid4().hex) is None                              # another chat
    assert recommendations.save(None, conv, relation="x", question="", loan_type="", city="", profile={}, shown=[],
                                seen=[], evaluations=[], sources=[], state="") is None


def test_deleting_a_conversation_deletes_its_recommendations():
    from app.main import app
    from app.services import accounts
    c = TestClient(app)
    r = c.post("/api/auth/register", json={"email": f"r-{uuid.uuid4().hex[:8]}@example.com", "name": "R",
                                           "password": "correct-horse"})
    uid = r.json()["user"]["id"]
    conv = c.post("/api/conversations", json={"title": "t", "data": {}}).json()["id"]
    recommendations.save(uid, conv, relation="new_profile", question=ASKED, loan_type="Home Loan", city="Pune",
                         profile={"fields": FIELDS}, shown=[], seen=[], evaluations=[], sources=[], state="")
    assert accounts.delete_conversation(uid, conv)
    assert recommendations.for_conversation(uid, conv) == []


def test_a_timeout_reaches_the_ui_as_a_temporary_failure(monkeypatch):
    from app.main import app
    from tests.test_chat_stream import _events
    c = TestClient(app)
    c.post("/api/auth/register", json={"email": f"t-{uuid.uuid4().hex[:8]}@example.com", "name": "T",
                                      "password": "correct-horse"})

    def slow(*a, **k):
        yield "status", {"stage": "search", "text": "Searching knowledge base"}
        raise TimeoutError("Request timed out")
    monkeypatch.setattr(chat, "stream", slow)
    ev = dict(_events(c.post("/api/chat/stream", json={"mode": "kb", "question": "HDFC LAP max loan?"}).text))
    assert ev["error"]["state"] == states.TEMPORARY_SERVICE_FAILURE
    assert "not a missing document" in ev["error"]["message"]


@pytest.mark.parametrize("q, ask", [("what is the ROI?", True), ("processing fee?", True), ("HDFC ROI", False),
                                    ("Orix India Finance code", False), ("what is FOIR for home loan", False)])
def test_a_lender_fact_without_any_lender_is_clarified(q, ask):
    assert followup.needs_lender(q, []) is ask
    route, _ = chat._route("kb", q, [], [], "")
    assert (route is not None and route.relation == "clarify" and route.direct == followup.ASK_WHICH) is ask


def test_the_chat_settles_which_lender_a_fact_is_about():
    hist = [{"role": "user", "text": "HDFC home loan max amount?"}, {"role": "assistant", "text": "₹10 crore [S1]."}]
    assert not followup.needs_lender("what is the ROI?", hist)
    hist += [{"role": "user", "text": "start a new case"}, {"role": "assistant", "text": followup.NEW_CASE_REPLY}]
    assert followup.needs_lender("what is the ROI?", hist)                 # a new case forgets HDFC


def test_their_numbers_ignore_lenders_the_model_picked_from_the_table(monkeypatch):
    _decided(monkeypatch, "contact_followup", lenders=["Axis Bank"])     # already shown with a number
    match = bankers.rank(bankers.Match(loan_type="Home Loan", city="Pune"), _rows(["Axis Bank"]), {AUG: {"title": "Aug"}}, {})
    match.also = ["Tata Capital"]
    r = followup.resolve("give me their contact numbers", _history(_customer(), match))
    assert r.lenders == ["Tata Capital"]


def test_only_a_shown_bankers_conflict_is_a_source_conflict(monkeypatch, no_search):
    monkeypatch.setattr(bankers, "find", _find(["Axis Bank"]))
    prep = chat.Prepared("kb", ASKED, {}, {}, None, [], [], "", use_llm=False, profile=_customer())
    post = prep.finish("Axis Bank: Ajit 8983240885 [S1]")
    prep.bankers.conflicts = 3                                            # among bankers not shown
    assert states.decide(prep, post, {"level": "high"})["state"] == states.ANSWER_VERIFIED
    prep.bankers.top[0].notes.append("verify: also listed as 9000000000")
    assert states.decide(prep, post, {"level": "high"})["state"] == states.SOURCE_CONFLICT
