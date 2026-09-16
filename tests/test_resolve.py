"""Voice/typed resolution flow with fake voice and fake prompts."""

import io

from rich.console import Console

from autoapply.ats.base import PendingQuestion
from autoapply.resolve import QuestionResolver


class FakeVoice:
    def __init__(self, transcripts):
        self.transcripts = list(transcripts)
        self.spoken: list[str] = []
        self.can_listen = True

    def say(self, text):
        self.spoken.append(text)

    def listen(self, on_start=None):
        return self.transcripts.pop(0) if self.transcripts else None


class FakeIO:
    def __init__(self, choices=(), texts=()):
        self.choices = list(choices)
        self.texts = list(texts)
        self.prompts: list[str] = []

    def choose(self, prompt, choices, default):
        self.prompts.append(prompt)
        return self.choices.pop(0) if self.choices else default

    def text(self, prompt, default=""):
        self.prompts.append(prompt)
        return self.texts.pop(0) if self.texts else default


class FormSpy:
    """Stands in for handler.apply_answer; records exactly which field got which value."""

    def __init__(self, fail_ids=()):
        self.applied: list[tuple[str, str]] = []
        self.fail_ids = set(fail_ids)
        self.submitted = False

    def apply(self, field_id, value):
        if field_id in self.fail_ids:
            return False, "boom"
        self.applied.append((field_id, value))
        return True, "ok"

    def submit(self):  # never called by the resolver
        self.submitted = True


def resolver(voice, io):
    return QuestionResolver(voice=voice, interaction=io, console=Console(file=io_buf(), width=100))


def io_buf():
    return io.StringIO()


def test_voice_answer_lands_in_the_right_field():
    pending = [
        PendingQuestion("f-why", "Why do you want to work at Duolingo?", "textarea", None, True, "company_motivation", "subjective"),
        PendingQuestion("f-src", "How did you hear about this position?", "text", None, True, "job_source", "left blank"),
    ]
    voice = FakeVoice(["I like products where engineering changes how people learn.", "LinkedIn."])
    form = FormSpy()
    res = resolver(voice, FakeIO(choices=["a", "a"])).resolve(pending, form.apply)
    assert [(r.field_id, r.action, r.source) for r in res] == [("f-why", "accepted", "voice"), ("f-src", "accepted", "voice")]
    assert form.applied == [
        ("f-why", "I like products where engineering changes how people learn."),
        ("f-src", "LinkedIn."),
    ]
    assert any("Why do you want to work at Duolingo?" in s for s in voice.spoken)
    assert form.submitted is False


def test_retry_then_edit_then_accept():
    pending = [PendingQuestion("f1", "Tell us about your experience in computer science", "textarea", None, True, "long_form", "")]
    voice = FakeVoice(["garbled first take", "second take"])
    form = FormSpy()
    io_ = FakeIO(choices=["r", "e"], texts=["second take, edited"])
    res = resolver(voice, io_).resolve(pending, form.apply)
    assert res[0].action == "accepted" and res[0].source == "typed"
    assert form.applied == [("f1", "second take, edited")]


def test_silence_then_type_and_skip():
    pending = [
        PendingQuestion("f1", "Pronounce your name?", "text", None, False, "personal_info", ""),
        PendingQuestion("f2", "Compensation expectation", "text", None, True, "salary", ""),
    ]
    voice = FakeVoice([None, None])  # nothing heard twice
    form = FormSpy()
    io_ = FakeIO(choices=["t", "s"], texts=["Div-ya-rith"])
    res = resolver(voice, io_).resolve(pending, form.apply)
    by = {r.field_id: r for r in res}
    assert by["f2"].action == "accepted" and by["f2"].value == "Div-ya-rith"  # required first, typed
    assert by["f1"].action == "skipped"
    assert form.applied == [("f2", "Div-ya-rith")]


def test_spoken_option_maps_to_option_text():
    pending = [PendingQuestion("sel", "Are you willing to work from the office?", "select", ["Yes", "No", "Prefer remote"], True, "availability", "")]
    form = FormSpy()
    res = resolver(FakeVoice(["yes, definitely"]), FakeIO(choices=["a"])).resolve(pending, form.apply)
    assert res[0].action == "accepted"
    assert form.applied == [("sel", "Yes")]
    form2 = FormSpy()
    res2 = resolver(FakeVoice(["option three"]), FakeIO(choices=["a"])).resolve(pending, form2.apply)
    assert form2.applied == [("sel", "Prefer remote")] and res2[0].action == "accepted"


def test_unmatched_spoken_option_falls_back_to_number():
    pending = [PendingQuestion("sel", "Term", "select", ["Summer 2027", "Fall 2027"], True, "availability", "")]
    form = FormSpy()
    io_ = FakeIO(choices=["n"], texts=["1"])
    res = resolver(FakeVoice(["the warm one"]), io_).resolve(pending, form.apply)
    assert form.applied == [("sel", "Summer 2027")] and res[0].action == "accepted"


def test_draft_accept_edit_and_skip():
    draft = "I built a 50K request/day LLM pipeline and want to work on learning systems at Duolingo's scale."
    mk = lambda fid: PendingQuestion(fid, "Why Duolingo?", "textarea", None, True, "company_motivation", "draft", draft=draft)
    form = FormSpy()
    res = resolver(None, FakeIO(choices=["a"])).resolve([mk("d1")], form.apply)
    assert res[0].action == "accepted" and res[0].source == "draft" and form.applied == [("d1", draft)]
    form = FormSpy()
    res = resolver(None, FakeIO(choices=["e"], texts=["my own words"])).resolve([mk("d2")], form.apply)
    assert form.applied == [("d2", "my own words")] and res[0].source == "typed"
    form = FormSpy()
    res = resolver(None, FakeIO(choices=["s"])).resolve([mk("d3")], form.apply)
    assert res[0].action == "skipped" and form.applied == [("d3", "")]  # draft cleared from the form


def test_typed_mode_without_voice_and_multiple_fields_keep_order_by_required():
    pending = [
        PendingQuestion("opt", "Website", "text", None, False, "contact", ""),
        PendingQuestion("req", "Why this role?", "textarea", None, True, "role_motivation", ""),
    ]
    form = FormSpy()
    io_ = FakeIO(texts=["because of the backend work", "https://example.com"])
    res = resolver(None, io_).resolve(pending, form.apply)
    assert [r.field_id for r in res] == ["req", "opt"]
    assert form.applied == [("req", "because of the backend work"), ("opt", "https://example.com")]


def test_apply_failure_is_reported_not_hidden():
    pending = [PendingQuestion("bad", "School", "combobox", None, True, "education", "")]
    form = FormSpy(fail_ids={"bad"})
    res = resolver(FakeVoice(["UMass"]), FakeIO(choices=["a", "s"])).resolve(pending, form.apply)
    assert res[0].action == "failed"
    assert form.applied == []


def test_resolver_never_submits():
    import inspect

    import autoapply.resolve as mod

    src = inspect.getsource(mod)
    assert "submit(" not in src.replace("never submits", "")
