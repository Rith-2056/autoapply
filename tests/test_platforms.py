from pathlib import Path

from autoapply.config import Profile
from autoapply.llm import JobContext
from autoapply.planner import Planner, Question
from autoapply.platforms import PlatformProfiles, normalize_question
from tests.test_planner import PROFILE, FakeLLM


def test_platform_overrides_and_recurring_answers(tmp_path: Path):
    pp = PlatformProfiles(tmp_path / "platforms.yaml")
    pp.set_fields("workday", {"education.major": "Computer Science", "name.preferred": "Rith"})
    pp.set_answer("greenhouse", "Are you willing to complete a background check? *", "Yes")
    # persisted and reloaded
    pp2 = PlatformProfiles(tmp_path / "platforms.yaml")
    assert pp2.field_override("workday", "education.major") == "Computer Science"
    assert pp2.field_override("greenhouse", "education.major") == ""
    assert pp2.answer_for("greenhouse", "Are you willing to complete a background check?") == "Yes"
    assert pp2.answer_for("workday", "Are you willing to complete a background check?") == ""
    assert normalize_question("  Why  Google? *") == "why google"
    fields = pp2.effective_fields("workday", PROFILE)
    major = next(f for f in fields if f["path"] == "education.major")
    assert major["override"] == "Computer Science"
    # clearing an answer removes it
    pp2.set_answer("greenhouse", "Are you willing to complete a background check? *", "")
    assert pp2.answer_for("greenhouse", "Are you willing to complete a background check?") == ""


def test_planner_uses_platform_profile_before_profile_and_llm(tmp_path: Path):
    pp = PlatformProfiles(tmp_path / "platforms.yaml")
    pp.set_fields("workday", {"name.first": "Div"})
    pp.set_answer("workday", "Are you willing to complete a background check?", "Yes")
    llm = FakeLLM({"Are you willing to complete a background check?": ("answer", "No", "high")})
    qs = [
        Question("a", "First Name", "text", None, True),
        Question("b", "Are you willing to complete a background check?", "select", ["Yes", "No"], True),
    ]
    dec = {d.question.id: d for d in Planner(PROFILE, llm, JobContext(), platform="workday", platforms=pp).plan(qs)}
    assert (dec["a"].value, dec["a"].source) == ("Div", "profile")  # platform field override wins over profile.yaml
    assert dec["b"].value == "Yes" and "answer profile" in dec["b"].reason
    assert not llm.calls  # configured answers never reach the LLM
    # other platforms are untouched
    dec2 = {d.question.id: d for d in Planner(PROFILE, llm, JobContext(), platform="greenhouse", platforms=pp).plan(qs)}
    assert dec2["a"].value == "Divyarith" and dec2["b"].value == "No"


def test_platform_questions_are_recorded(tmp_path: Path):
    from autoapply.tracker import Tracker

    t = Tracker(tmp_path / "t.db")
    t.record_platform_question("greenhouse", "why us", "Why us?", "company_motivation", "textarea", None, "", "needs_user")
    t.record_platform_question("greenhouse", "why us", "Why us?", "company_motivation", "textarea", None, "Because.", "voice")
    rows = t.platform_questions("greenhouse")
    assert rows[0]["seen"] == 2 and rows[0]["last_value"] == "Because." and rows[0]["last_source"] == "voice"
    assert t.platform_questions("workday") == []
