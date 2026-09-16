"""Planner tests with a fake LLM: what gets filled, drafted, or handed to the user."""

from autoapply.config import Profile
from autoapply.llm import BatchQuestion, JobContext, LLMDecision, QuestionAnswerer
from autoapply.planner import Planner, Question

PROFILE = Profile(
    {
        "name": {"first": "Divyarith", "last": "Shivashok", "full": "Divyarith Shivashok"},
        "contact": {"email": "d@example.com", "phone": "669-249-4174"},
        "education": {
            "school": "University of Massachusetts Amherst", "degree": "Bachelor of Science", "gpa": "3.93",
            "start_month": "August", "start_year": 2024, "graduation_month": "May", "graduation_year": 2028, "graduation": "May 2028",
        },
        "work_authorization": {"authorized_us": "Yes", "requires_sponsorship": "No", "willing_to_relocate": "Yes"},
        "eeo": {"gender": "Male", "race": "Asian (South Asian)", "sexual_orientation": "Heterosexual / Straight",
                "hispanic": "No, not Hispanic or Latino", "transgender": "[FILL IN]", "disability": "No, I do not have a disability"},
        "preferences": {"how_did_you_hear": "", "consent": "Yes"},
    }
)


class FakeLLM(QuestionAnswerer):
    """Returns scripted decisions and records what it was asked."""

    def __init__(self, script=None, enabled=True):
        super().__init__(api_key="k" if enabled else "", model="fake", resume_text="resume", profile_summary="profile")
        self.script = script or {}
        self.calls: list[list[BatchQuestion]] = []

    def resolve_batch(self, questions, job=None):
        self.calls.append(questions)
        out = {}
        for q in questions:
            d = self.script.get(q.label)
            if d is None:
                out[q.id] = LLMDecision(q.id, "ask_user", reason="scripted: unknown")
            else:
                dec = LLMDecision(q.id, d[0], d[1], d[2] if len(d) > 2 else "high", "scripted")
                if dec.decision == "answer" and q.options:
                    dec = self._validate_options(dec, q)
                if dec.decision == "answer" and not dec.accepted(self.min_confidence):
                    dec.decision = "ask_user"
                out[q.id] = dec
        return out


def plan(questions, llm=None):
    return {d.question.id: d for d in Planner(PROFILE, llm or FakeLLM(), JobContext(company="Duolingo", role="SWE Intern")).plan(questions)}


def test_education_dates_from_profile():
    months = ["January", "May", "August", "December"]
    years = ["2024", "2026", "2028"]
    d = plan([
        Question("a", "Start date month", "select", months, True),
        Question("b", "Start date year", "select", years, True),
        Question("c", "End date month", "select", months, True),
        Question("d", "End date year", "select", years, True),
        Question("e", "What is your expected graduation timeline?", "combobox", ["Fall 2027", "Spring 2028", "Fall 2028"], True),
        Question("f", "Undergraduate GPA", "text", None, True),
        Question("g", "School", "combobox", None, True),
    ])
    assert (d["a"].status, d["a"].value) == ("filled", "August")
    assert (d["b"].status, d["b"].value) == ("filled", "2024")
    assert (d["c"].status, d["c"].value) == ("filled", "May")
    assert (d["d"].status, d["d"].value) == ("filled", "2028")
    assert (d["e"].status, d["e"].value) == ("filled", "Spring 2028")
    assert (d["f"].status, d["f"].value) == ("filled", "3.93")
    assert (d["g"].status, d["g"].value) == ("filled", "University of Massachusetts Amherst")
    assert all(x.source == "profile" for x in d.values())


def test_demographics_from_profile_and_unknown_asked():
    llm = FakeLLM()
    d = plan([
        Question("g", "How would you describe your gender identity? (mark all that apply)", "checkbox_group", ["Man", "Woman", "Non-binary", "Prefer not to say"], False),
        Question("r", "How would you describe your racial/ethnic background?", "select", ["Asian", "Black or African American", "White", "Prefer not to say"], False),
        Question("s", "How would you describe your sexual orientation?", "select", ["Heterosexual or straight", "Gay or lesbian", "Prefer not to say"], False),
        Question("t", "Do you identify as transgender?", "select", ["Yes", "No", "Prefer not to say"], False),
        Question("h", "Are you Hispanic or Latino?", "select", ["Yes", "No"], False),
        Question("dis", "Do you have a disability or chronic condition?", "select", ["Yes", "No", "Prefer not to say"], False),
    ], llm)
    assert (d["g"].status, d["g"].value) == ("filled", "Man")
    assert (d["r"].status, d["r"].value) == ("filled", "Asian")
    assert (d["s"].status, d["s"].value) == ("filled", "Heterosexual or straight")
    assert (d["h"].status, d["h"].value) == ("filled", "No")
    assert (d["dis"].status, d["dis"].value) == ("filled", "No")
    # transgender status was not provided -> asked, and never sent to the LLM
    assert d["t"].status == "needs_user"
    assert not llm.calls, "demographics must never be inferred by the LLM"


def test_job_source_stays_blank_even_if_llm_would_answer():
    llm = FakeLLM({"Where did you hear about this job?": ("answer", "LinkedIn")})
    d = plan([Question("src", "Where did you hear about this job?", "select", ["LinkedIn", "Handshake", "Other"], True)], llm)
    assert d["src"].status == "needs_user"
    assert "only you know" in d["src"].reason
    assert not llm.calls


def test_salary_and_personal_preferences_are_asked():
    d = plan([
        Question("pay", "What is your compensation expectation?", "text", None, True),
        Question("pron", "How do you pronounce your name?", "text", None, False),
        Question("acct", "Do you have a Duolingo account? If yes, what is your username?", "text", None, False),
    ])
    assert d["pay"].status == "needs_user"
    assert d["pron"].status == "needs_user"
    assert d["acct"].status == "needs_user"  # FakeLLM has no script -> ask_user


def test_motivation_questions_become_drafts_requiring_approval():
    llm = FakeLLM({"Why Duolingo?": ("draft", "I built an LLM structured-output pipeline at FishEye...")})
    d = plan([Question("why", "Why Duolingo?", "textarea", None, True)], llm)
    assert d["why"].status == "draft"
    assert d["why"].source == "draft"
    assert d["why"].value.startswith("I built")
    assert llm.calls and llm.calls[0][0].policy == "draft"


def test_reasonable_inference_is_filled_and_low_confidence_is_asked():
    llm = FakeLLM({
        "Have you previously completed at least 1 internship or have relevant full-time experience?": ("answer", "Yes", "medium"),
        "Years of relevant experience?": ("answer", "2", "low"),
        "Which technical domains are you most interested in?": ("answer", "Backend || Machine Learning", "medium"),
    })
    d = plan([
        Question("i", "Have you previously completed at least 1 internship or have relevant full-time experience?", "combobox", ["Yes", "No"], True),
        Question("y", "Years of relevant experience?", "text", None, True),
        Question("dom", "Which technical domains are you most interested in?", "checkbox_group", ["Backend", "Frontend", "Machine Learning"], True, multi=True),
    ], llm)
    assert (d["i"].status, d["i"].value, d["i"].source) == ("filled", "Yes", "inferred")
    assert d["y"].status == "needs_user"
    assert (d["dom"].status, d["dom"].value) == ("filled", "Backend || Machine Learning")


def test_work_authorization_is_strict():
    llm = FakeLLM({"After the OPT, are you eligible for a 24-month OPT STEM extension?": ("answer", "Yes", "medium")})
    d = plan([
        Question("us", "Are you authorized to work lawfully in the United States?", "select", ["Yes", "No"], True),
        Question("sp", "Will you now, or in the future, require sponsorship?", "select", ["Yes", "No"], True),
        Question("opt", "After the OPT, are you eligible for a 24-month OPT STEM extension?", "select", ["Yes", "No", "N/A"], True),
    ], llm)
    assert (d["us"].status, d["us"].value) == ("filled", "Yes")
    assert (d["sp"].status, d["sp"].value) == ("filled", "No")
    assert llm.calls[0][0].policy == "strict"
    assert d["opt"].status == "filled"  # the (fake) LLM chose to answer under strict policy


def test_no_api_key_means_ask_user_not_silence():
    d = plan([Question("q", "Tell us about your experience in computer science", "textarea", None, True)], FakeLLM(enabled=False))
    assert d["q"].status == "needs_user"
    assert "ANTHROPIC_API_KEY" in d["q"].reason


def test_every_question_gets_exactly_one_decision():
    qs = [Question(str(i), f"Question {i}?", "text", None, i % 2 == 0) for i in range(12)]
    decisions = Planner(PROFILE, FakeLLM(), None).plan(qs)
    assert [d.question.id for d in decisions] == [q.id for q in qs]
    assert all(d.status in ("filled", "draft", "needs_user", "n/a") for d in decisions)


def test_option_validation_rejects_non_options():
    q = BatchQuestion(id="x", label="Pick", options=["Red", "Blue"])
    d = QuestionAnswerer._validate_options(LLMDecision("x", "answer", "green", "high"), q)
    assert d.decision == "ask_user"
    d2 = QuestionAnswerer._validate_options(LLMDecision("x", "answer", "blue", "high"), q)
    assert (d2.decision, d2.answer) == ("answer", "Blue")
