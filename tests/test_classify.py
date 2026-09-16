import pytest

from autoapply.classify import Category, Policy, classify, policy_for


@pytest.mark.parametrize(
    "label,expected",
    [
        ("First Name", Category.PERSONAL),
        ("How do you pronounce your name?", Category.PERSONAL),
        ("Email", Category.CONTACT),
        ("LinkedIn Profile", Category.CONTACT),
        ("School", Category.EDUCATION),
        ("End date month", Category.EDUCATION),
        ("What is your expected graduation timeline?", Category.EDUCATION),
        ("Undergraduate GPA", Category.EDUCATION),
        ("Have you received a competitive academic scholarship, fellowship, or academic honor?", Category.EDUCATION),
        ("Years of relevant experience?", Category.EMPLOYMENT),
        ("Have you previously completed at least 1 internship or have relevant full-time experience?", Category.EMPLOYMENT),
        ("Which technical domains are you most interested in?", Category.SKILLS),
        ("Are you authorized to work lawfully in the United States?", Category.WORK_AUTH),
        ("Will you now, or in the future, require sponsorship?", Category.SPONSORSHIP),
        ("After the OPT, are you eligible for a 24-month OPT STEM extension?", Category.SPONSORSHIP),
        ("How would you describe your gender identity?", Category.DEMOGRAPHICS),
        ("How would you describe your racial/ethnic background?", Category.DEMOGRAPHICS),
        ("Do you identify as transgender?", Category.DEMOGRAPHICS),
        ("Do you have a disability or chronic condition?", Category.DEMOGRAPHICS),
        ("Are you willing to work from the office location?", Category.AVAILABILITY),
        ("What is your compensation expectation?", Category.SALARY),
        ("Where did you hear about this job?", Category.JOB_SOURCE),
        ("How did you hear about us?", Category.JOB_SOURCE),
        ("Why Duolingo?", Category.COMPANY_MOTIVATION),
        ("Why do you want to work at Duolingo?", Category.COMPANY_MOTIVATION),
        ("What motivated you to apply?", Category.COMPANY_MOTIVATION),
        ("Why are you interested in this role?", Category.ROLE_MOTIVATION),
        ("What excites you about this opportunity?", Category.ROLE_MOTIVATION),
        ("Share with us your proudest accomplishment.", Category.BEHAVIORAL),
        ("How will your personal experiences make an impact at Duolingo?", Category.BEHAVIORAL),
        ("Tell us about a time you disagreed with a teammate", Category.BEHAVIORAL),
        ("Applicant Privacy Acknowledgement", Category.CONSENT),
        ("Do you have a Duolingo account? If yes, what is your username?", Category.SHORT_ANSWER),
        ("Is there anything else not listed on your resume you'd like to share?", Category.SHORT_ANSWER),
    ],
)
def test_classify(label, expected):
    assert classify(label) == expected


def test_opt_out_is_not_a_visa_question():
    assert classify("Would you like to receive SMS updates? Reply STOP to opt out.") in (Category.SHORT_ANSWER, Category.OTHER)
    assert classify("Are you currently on OPT?") == Category.SPONSORSHIP


def test_textarea_defaults_to_long_form():
    assert classify("Additional information", field_type="textarea") == Category.LONG_FORM
    assert classify("Notes") == Category.OTHER


def test_policies():
    assert policy_for(Category.JOB_SOURCE) == Policy.BLANK
    assert policy_for(Category.COMPANY_MOTIVATION) == Policy.DRAFT
    assert policy_for(Category.BEHAVIORAL) == Policy.DRAFT
    assert policy_for(Category.SALARY) == Policy.ASK
    assert policy_for(Category.EDUCATION) == Policy.INFER
    assert policy_for(Category.SKILLS) == Policy.INFER
    assert policy_for(Category.WORK_AUTH) == Policy.STRICT
    assert policy_for(Category.DEMOGRAPHICS) == Policy.PROFILE
