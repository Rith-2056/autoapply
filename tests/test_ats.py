import pytest

from autoapply.ats import ATS_TYPES, detect_ats


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://job-boards.greenhouse.io/doordashcanada/jobs/8170944?utm_source=Simplify", "greenhouse"),
        ("https://boards.greenhouse.io/acme/jobs/123", "greenhouse"),
        ("https://job-boards.eu.greenhouse.io/acme/jobs/1", "greenhouse"),
        ("https://www.acme.com/careers/job?gh_jid=4567", "greenhouse"),
        ("https://acme.com/careers/gh_jid/4567", "greenhouse"),
        ("https://jobs.lever.co/acme/8d7f-1234", "lever"),
        ("https://jobs.eu.lever.co/acme/8d7f-1234/apply", "lever"),
        ("https://jobs.ashbyhq.com/acme/0d1e-abc", "ashby"),
        ("https://gdit.wd5.myworkdayjobs.com/external_career_site/job/USA/Intern_RQ1", "workday"),
        ("https://acme.wd1.myworkdayjobs.com/en-US/careers/job/X", "workday"),
        ("https://jobs.smartrecruiters.com/Acme/74400012-software-intern", "smartrecruiters"),
        ("https://careers.acme.com/job/123?ats=successfactors", "generic"),
        ("https://egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/1", "generic"),
        ("https://careers-sig.icims.com/jobs/1/job", "generic"),
        ("", "generic"),
        ("not a url", "generic"),
        ("https://notgreenhouse.io.evil.com/jobs/1", "generic"),
    ],
)
def test_detect_ats(url, expected):
    assert detect_ats(url) == expected


def test_all_types_have_handlers():
    from autoapply.ats import get_handler_class

    for t in ATS_TYPES:
        cls = get_handler_class(t)
        assert cls.name == t
