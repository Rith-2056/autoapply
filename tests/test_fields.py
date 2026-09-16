from autoapply.config import Profile
from autoapply.fields import choose_month, choose_option, clean_label, match_rule, profile_value, should_skip, yes_no

PROFILE = Profile(
    {
        "name": {"first": "Divyarith", "last": "Shivashok", "full": "Divyarith Shivashok"},
        "contact": {"email": "d@example.com", "phone": "669-249-4174", "linkedin": "https://linkedin.com/in/x", "github": "https://github.com/x"},
        "education": {"school": "UMass Amherst", "gpa": "3.93", "graduation_month": "May", "graduation_year": 2028, "degree": "Bachelor of Science"},
        "work_authorization": {"authorized_us": "Yes, authorized", "requires_sponsorship": "[FILL IN]"},
        "eeo": {"gender": "Male", "veteran": "I am not a protected veteran"},
        "address": {"location": "[FILL IN]"},
    }
)


def test_clean_label():
    assert clean_label("First Name *") == "First Name"
    assert clean_label("  Email (required):") == "Email"
    assert clean_label("LinkedIn Profile (optional)") == "LinkedIn Profile"


def test_match_rule_paths():
    assert match_rule("First Name *").path == "name.first"
    assert match_rule("Last name").path == "name.last"
    assert match_rule("Email").path == "contact.email"
    assert match_rule("Phone number").path == "contact.phone"
    assert match_rule("LinkedIn Profile").path == "contact.linkedin"
    assert match_rule("GitHub URL").path == "contact.github"
    assert match_rule("School").path == "education.school"
    assert match_rule("Expected graduation year").path == "education.graduation_year"
    assert match_rule("Are you legally authorized to work in the United States?").path == "work_authorization.authorized_us"
    assert match_rule("Will you now or in the future require sponsorship for employment visa status?").path == "work_authorization.requires_sponsorship"
    assert match_rule("Gender").path == "eeo.gender"
    assert match_rule("Veteran Status").path == "eeo.veteran"
    assert match_rule("How did you hear about this job?").path == "preferences.how_did_you_hear"
    assert match_rule("Tell us about a project you are proud of") is None
    assert should_skip("Cover Letter")
    assert not should_skip("Pronouns")  # asked, never silently skipped
    assert match_rule("End date month").path == "education.graduation_month"
    assert match_rule("End date year").path == "education.graduation_year"
    assert match_rule("Start date month").path == "education.start_month"
    assert match_rule("Start date year").path == "education.start_year"
    assert match_rule("Start date").path == "preferences.available_start"
    assert match_rule("Alternate Email") is None
    assert match_rule("Are you willing to work from the office location in Pittsburgh?").path == "work_authorization.willing_to_relocate"
    assert match_rule("How would you describe your sexual orientation?").path == "eeo.sexual_orientation"
    assert match_rule("Do you identify as transgender?").path == "eeo.transgender"


def test_profile_value_and_placeholders():
    assert profile_value(PROFILE, match_rule("Email")) == "d@example.com"
    # [FILL IN] placeholder must never be typed into a form
    assert profile_value(PROFILE, match_rule("Do you require visa sponsorship?")) == ""
    assert profile_value(PROFILE, match_rule("Current location")) == ""
    assert PROFILE.placeholders() == ["work_authorization.requires_sponsorship", "address.location"]


def test_yes_no_and_choose_option():
    assert yes_no("Yes, I am authorized") == "Yes"
    assert yes_no("No") == "No"
    assert yes_no("Maybe") is None
    assert choose_option(["Yes", "No"], "Yes, authorized", "yesno") == "Yes"
    assert choose_option(["Male", "Female", "Decline To Self Identify"], "Male", "select") == "Male"
    assert choose_option(["Female", "Male"], "Male", "select") == "Male"
    assert choose_option(["I am not a protected veteran", "I identify as a veteran"], "I am not a protected veteran") == "I am not a protected veteran"
    assert choose_option(["Bachelor's Degree", "Master's Degree"], "Bachelor of Science", "select") == "Bachelor's Degree"
    assert choose_option(["Red", "Blue"], "Green") is None
    assert choose_option(["Decline to self-identify", "Asian"], "decline") == "Decline to self-identify"


def test_choose_month():
    assert choose_month(["January", "May", "June"], "May") == "May"
    assert choose_month(["01", "05"], "May") == "05"
    assert choose_month(["Jan", "Feb"], "May") is None


def test_identity_rules_ignore_long_questions():
    # A long question mentioning "email" must not be treated as the email field.
    long_q = ("Would you like to receive communications via SMS and/or WhatsApp about your application? "
              "If you select no, we will only communicate with you via email.")
    r = match_rule(long_q)
    assert r is not None and r.path == "preferences.sms_opt_in"
    assert match_rule("Email Address").path == "contact.email"
    assert match_rule("Have you worked at DoorDash?").path == "preferences.previously_employed_here"
    assert match_rule("Applicant Privacy Acknowledgement").kind == "consent"
    # Work authorization for another country must not map to the US answer.
    assert match_rule("Are you legally eligible to work in Canada?") is None
    assert match_rule("Are you legally authorized to work in the United States?").path == "work_authorization.authorized_us"
    assert match_rule("Are you authorized to work in the US? (Canada roles: see below)").path == "work_authorization.authorized_us"


def test_numeric_range_and_consent():
    from autoapply.fields import choose_consent, choose_numeric_range

    assert choose_numeric_range(["3.75+", "3.41 - 3.74", "Below 3.4"], "3.93") == "3.75+"
    assert choose_numeric_range(["3.75+", "3.41 - 3.74", "Below 3.4"], "3.5") == "3.41 - 3.74"
    assert choose_numeric_range(["3.75+", "3.41 - 3.74", "Below 3.4"], "3.0") == "Below 3.4"
    assert choose_numeric_range(["3.5 or higher", "Under 3.5"], "3.93") == "3.5 or higher"
    assert choose_numeric_range(["A", "B"], "3.9") is None
    assert choose_consent(["Select...", "I acknowledge", "I do not acknowledge"]) == "I acknowledge"
    assert choose_consent(["Yes", "No"]) == "Yes"
    assert choose_consent(["Red", "Blue"]) is None


def test_clean_label_strips_option_text():
    assert clean_label("Gender Select ... Male Female Decline to self-identify") == "Gender"
    assert clean_label("Veteran status Select ... I identify as one or more") == "Veteran status"
    assert clean_label("Preferred name") == "Preferred name"


def test_choose_graduation():
    from autoapply.fields import choose_graduation

    opts = ["Fall 2027", "Spring 2028", "Summer 2028", "Fall 2028"]
    assert choose_graduation(opts, "May", 2028) == "Spring 2028"
    assert choose_graduation(["2027", "2028", "2029"], "May", "2028") == "2028"
    assert choose_graduation(["May 2028", "December 2028"], "May", 2028) == "May 2028"
    assert choose_graduation(["2027-2028", "2028-2029"], "May", 2028) is None  # ambiguous
    assert choose_graduation(["2027"], "May", 2028) is None
