from autoapply.dictation import DictationContext, TranscriptCleaner, guard_no_invention, vocabulary_from_text

RESUME = "FishEye Software, Inc. Engineered LLM pipeline across local HDF5 and AWS S3. Designed data lake API in C++ bridging REST, gRPC and WebSocket. Pydantic, PyTorch, Drake, DynamoDB."


def cleaner():
    return TranscriptCleaner(api_key="", use_llm=False)


def test_context_corrects_homophones_without_llm():
    r = cleaner().clean("I worked at fish eye software building the data lack", DictationContext(resume_text=RESUME))
    assert r.cleaned == "I worked at FishEye Software building the data lake."
    assert not r.ai_modified and r.confidence in ("high", "medium")


def test_tech_terms_and_fillers():
    r = cleaner().clean("um I used AWS lamba and dynamo db, you know, with pie torch", DictationContext(resume_text=RESUME))
    assert "Lambda" in r.cleaned and "DynamoDB" in r.cleaned and "PyTorch" in r.cleaned
    assert "um" not in r.cleaned.lower().split() and "you know" not in r.cleaned


def test_uncertain_span_gets_suggestion_not_silent_change():
    r = cleaner().clean("I built a service with kubernattis and terraform", DictationContext(resume_text=RESUME))
    # 'kubernattis' resembles Kubernetes but not closely enough to auto-correct: flag it with a suggestion
    sugg = {s.text.lower(): s.suggestion for s in r.spans}
    assert "kubernattis" in sugg and sugg["kubernattis"] == "Kubernetes"
    assert "kubernattis" in r.cleaned  # not silently replaced
    assert r.confidence == "medium"


def test_guard_rejects_added_content():
    raw = "I built a data lake at FishEye that ingested telemetry"
    ok, added = guard_no_invention(raw, "I built a data lake at FishEye that ingested telemetry.")
    assert ok
    ok, added = guard_no_invention(raw, "I built a data lake at FishEye that ingested telemetry and led a team of five engineers using Kubernetes and Spark.")
    assert not ok and "kubernetes" in added


def test_llm_result_that_invents_is_rejected_and_raw_kept():
    raw = "I worked on the data lake and the pydantic validation"
    r = cleaner().clean(raw, DictationContext(resume_text=RESUME), llm_result={"cleaned": "I architected the entire data lake platform, led the Pydantic validation effort and mentored three interns.", "uncertain": [], "confidence": "high"})
    assert r.guard_rejected and r.confidence == "low"
    assert "mentored" not in r.cleaned and "data lake" in r.cleaned


def test_llm_result_that_only_edits_is_accepted():
    raw = "i worked on the data lack and pie dantic validation"
    r = cleaner().clean(raw, DictationContext(resume_text=RESUME), llm_result={"cleaned": "I worked on the data lake and Pydantic validation.", "uncertain": [{"text": "validation", "suggestion": "validation"}], "confidence": "high"})
    assert r.cleaned == "I worked on the data lake and Pydantic validation." and r.ai_modified
    assert r.raw == raw


def test_vocabulary_extraction():
    v = vocabulary_from_text(RESUME)
    assert "FishEye" in v and "Pydantic" in v and "HDF5" in v and "C++" in v


def test_empty_and_short():
    assert cleaner().clean("   ").cleaned == ""
    assert cleaner().clean("yes").confidence == "medium"
