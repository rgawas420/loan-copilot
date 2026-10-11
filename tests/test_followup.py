"""Follow-ups in one chat: the customer discussed earlier is read back from the app's own profile table, a new
detail ("city pune") updates it, a lender ("tata capital") narrows its bankers, and a new question drops the
conversation."""
import app.rag.bankers as bankers
import app.rag.chat as chat
import app.rag.followup as followup
import app.rag.llm as llm
import app.rag.profile as profile
import app.rag.retrieval as retrieval

ASKED = ("Which bank offers a home loan to a customer earning ₹45,000, having a CIBIL score of 780, no existing "
         "EMIs, and an unlisted employer?")
FIELDS = {"loan_type": "Home Loan", "employment": "unlisted employer", "monthly_income": "₹45,000", "cibil": "780",
          "existing_emi": "None"}


def _asked_for_city():
    p = profile.Profile(question=ASKED, fields=dict(FIELDS))
    p.calc = profile.compute(p.fields)
    return p.ask_answer()                       # the app's reply: profile table, eligibility, "needs the city"


HISTORY = [{"role": "user", "text": ASKED}, {"role": "assistant", "text": _asked_for_city()}]


def _decided(monkeypatch, relation, lenders=()):
    monkeypatch.setattr(llm, "get_llm", lambda kind="fast", max_tokens=None: object())
    monkeypatch.setattr(followup, "_known_banks", lambda: [])
    monkeypatch.setattr(followup, "_decide", lambda m, q, h, a: followup._Decision(relation=relation, lenders=list(lenders)))


def test_the_profile_table_is_read_back_without_not_provided():
    fields, other = followup.read_table(_asked_for_city())
    assert fields == FIELDS and other == []


def test_the_customer_stays_active_only_while_the_last_answer_is_about_them():
    a = followup.active(HISTORY)
    assert a.question == ASKED and a.fields == FIELDS
    later = [{"role": "user", "text": "HDFC LAP max loan?"}, {"role": "assistant", "text": "Up to ₹5 crore [S1]."}]
    assert followup.active(HISTORY + later) is None         # "and for HDFC?" now follows the LAP question
    from app.rag.grounding import NOT_FOUND
    missed = [{"role": "user", "text": "city pune"}, {"role": "assistant", "text": NOT_FOUND}]
    assert followup.active(HISTORY + missed).fields == FIELDS     # a "not found" turn added nothing


def test_a_new_detail_updates_the_earlier_customer(monkeypatch):
    _decided(monkeypatch, "update_profile")
    monkeypatch.setattr(profile, "extract", lambda q: profile.Profile(question=q, fields={"property_location": "Pune"}))
    r = followup.resolve("city pune", HISTORY)
    assert r.relation == "update_profile"
    assert r.profile.fields == {**FIELDS, "property_location": "Pune"} and r.profile.city == "Pune"
    assert ASKED in r.profile.question and "city pune" in r.profile.question
    assert r.profile.calc.get("table")                          # eligibility recalculated


def test_a_changed_detail_replaces_the_old_one(monkeypatch):
    _decided(monkeypatch, "update_profile")
    monkeypatch.setattr(profile, "extract", lambda q: profile.Profile(question=q, fields={"cibil": "710"}))
    assert followup.resolve("cibil is 710 now", HISTORY).profile.fields["cibil"] == "710"


def test_a_lender_narrows_the_bankers_for_the_same_customer(monkeypatch):
    _decided(monkeypatch, "ask_lender", ["tata capital"])
    hist = HISTORY + [{"role": "user", "text": "city pune"},
                      {"role": "assistant", "text": _asked_for_city().replace("| Property Location | Not provided |",
                                                                             "| Property Location | Pune |")}]
    r = followup.resolve("tata capital", hist)
    assert r.relation == "ask_lender" and r.lenders == ["Tata Capital"]
    assert r.profile.lenders == ["Tata Capital"] and r.profile.city == "Pune"
    assert r.profile.search.startswith("Tata Capital Home Loan Pune")


def test_a_lender_nobody_named_is_not_used(monkeypatch):
    _decided(monkeypatch, "ask_lender", ["HDFC Bank"])          # the model's guess for "the second one"
    r = followup.resolve("and the second one?", HISTORY)
    assert r.relation == "followup" and r.profile is None


def test_without_an_earlier_profile_a_detail_is_a_plain_followup(monkeypatch):
    _decided(monkeypatch, "update_profile")
    r = followup.resolve("city pune", [{"role": "user", "text": "HDFC LAP max loan?"},
                                        {"role": "assistant", "text": "Up to ₹5 crore [S1]."}])
    assert r.relation == "followup" and r.profile is None


def test_a_new_question_is_answered_without_the_conversation(monkeypatch):
    _decided(monkeypatch, "new")
    route, history = chat._route("kb", "Bajaj LAP RM contact in Mumbai?", HISTORY, [], "")
    assert route.relation == "new" and history == []


def test_nothing_is_routed_without_a_conversation_or_a_model(monkeypatch):
    assert followup.resolve("city pune", []) is None
    monkeypatch.setattr(llm, "get_llm", lambda kind="fast", max_tokens=None: None)
    assert followup.resolve("city pune", HISTORY) is None
    assert chat._route("ask", "why not HDFC?", HISTORY, [], "") == (None, HISTORY)    # recommendation follow-ups


def test_a_followup_profile_matches_only_the_lenders_asked_about(monkeypatch):
    calls = []
    monkeypatch.setattr(retrieval, "vector_hits", lambda q, k, lt, banks=None: [])
    monkeypatch.setattr(retrieval, "keyword_hits", lambda q, k, exact: [])
    monkeypatch.setattr(bankers, "find", lambda lt, city, lenders=None, exclude=None: calls.append((lt, city, lenders)) or
                        bankers.Match(loan_type=lt, city=city, lenders_filter=list(lenders or [])))
    p = profile.Profile(question=f"{ASKED}\n\nFollow-up from the partner: tata capital",
                        fields={**FIELDS, "property_location": "Pune"}, lenders=["Tata Capital"])
    p.calc = profile.compute(p.fields)
    prep = chat.Prepared("kb", "tata capital", {}, {}, None, HISTORY, [], "", use_llm=False, profile=p)
    assert calls == [("Home Loan", "Pune", ["Tata Capital"])]
    prompt = "\n".join(m.content for m in prep.messages)
    assert ASKED in prompt and "Pune at Tata Capital" in prompt and not prep.not_found


def test_a_lender_question_about_another_fact_or_product_is_new(monkeypatch):
    act = followup.active(HISTORY)
    for q in ("What is the Kotak Mahindra code for medical equipment loan?", "HDFC payout for home loan",
              "Bajaj LAP RM contact in Mumbai?"):
        assert followup._own_question(q, act), q
    for q in ("tata capital", "what about HDFC?", "any Bajaj RM?", "Bajaj home loan RM?"):
        assert not followup._own_question(q, act), q
    _decided(monkeypatch, "ask_lender", ["Kotak Mahindra Bank"])
    r = followup.resolve("What is the Kotak Mahindra code for medical equipment loan?", HISTORY)
    assert r.relation == "new" and r.profile is None
