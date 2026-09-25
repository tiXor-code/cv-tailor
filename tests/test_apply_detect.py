from cv_tailor.apply_detect import detect_apply_channel

def test_send_cv_to():
    m, t = detect_apply_channel("To apply, send your CV to jobs@acme.dev with the subject AI.")
    assert (m, t) == ("email", "jobs@acme.dev")

def test_mailto():
    m, t = detect_apply_channel('Apply here: <a href="mailto:talent@startup.io">email us</a>')
    assert (m, t) == ("email", "talent@startup.io")

def test_applications_to():
    m, t = detect_apply_channel("Applications to hiring@corp.com by Friday.")
    assert (m, t) == ("email", "hiring@corp.com")

def test_plain_contact_email_is_not_apply():
    # an email that appears without an application verb context stays portal
    m, t = detect_apply_channel("Questions? Reach us at info@acme.dev. Apply on our site.")
    assert (m, t) == ("portal", None)

def test_multiple_candidates_prefers_company_domain():
    d = "Send your resume to recruiter@agency.com or careers@acme.dev"
    m, t = detect_apply_channel(d, company_domain="acme.dev")
    assert (m, t) == ("email", "careers@acme.dev")

def test_multiple_candidates_no_company_domain_is_ambiguous():
    d = "Send your resume to recruiter@agency.com. Applications to careers@other.io."
    assert detect_apply_channel(d) == ("portal", None)

def test_noreply_filtered():
    assert detect_apply_channel("Send your CV to noreply@acme.dev") == ("portal", None)

def test_empty():
    assert detect_apply_channel("") == ("portal", None)

def test_applications_to_two_or_joined_is_ambiguous():
    assert detect_apply_channel("Applications to careers@acme.dev or hr@acme.dev.") == ("portal", None)

def test_cc_address_is_not_a_candidate():
    m, t = detect_apply_channel(
        "Send your CV to jobs@thirdparty.com, cc info@acme.dev for visibility.",
        company_domain="acme.dev")
    assert (m, t) == ("email", "jobs@thirdparty.com")

def test_single_line_second_sentence_email_does_not_downgrade():
    d = "Send your CV to jobs@acme.dev to apply. Questions? reach out to hello@acme.dev anytime."
    assert detect_apply_channel(d) == ("email", "jobs@acme.dev")

def test_slash_joined_no_space_is_ambiguous():
    assert detect_apply_channel("Apply via careers@acme.dev/hr@acme.dev") == ("portal", None)

def test_slash_joined_no_space_company_domain_wins():
    m, t = detect_apply_channel("Send your CV to jobs@thirdparty.com/careers@acme.dev please",
                                company_domain="acme.dev")
    assert (m, t) == ("email", "careers@acme.dev")

def test_or_without_trailing_space_does_not_glue():
    # 'or' with no space after it must NOT be treated as a joiner
    assert detect_apply_channel("Send your CV to jobs@acme.dev orbit@space.dev is unrelated") == ("email", "jobs@acme.dev")


# --- Hacker News phrasings (2026-09-25): 146 of 256 September posts took
# applications by email; the detector caught 8. ---

from cv_tailor.apply_detect import detect_apply_channel as _d


def test_hn_style_email_instructions_are_detected():
    for text, want in (
        ("Great team. Contact: ada@fixture.example", "ada@fixture.example"),
        ("No visa sponsorship. To apply: hiring@fixture.example Subject: HN Hiring", "hiring@fixture.example"),
        ("Interested? email jobs@fixture.example with your GitHub", "jobs@fixture.example"),
        ("To apply, contact ada@fixture.example with the subject line HN", "ada@fixture.example"),
        ("How to apply: Email ada@fixture.example with your CV", "ada@fixture.example"),
    ):
        assert _d(text) == ("email", want), text


def test_obfuscated_addresses_are_decoded():
    assert _d("Apply: jobs [at] fixture [dot] example") == ("email", "jobs@fixture.example")
    assert _d("Email ada (at) fixture (dot) example") == ("email", "ada@fixture.example")
    assert _d("Contact: ada at fixture dot example") == ("email", "ada@fixture.example")


def test_non_application_addresses_stay_ignored():
    assert _d("Questions about privacy? Contact: privacy@fixture.example") == ("portal", None)
    assert _d("We ship at scale and our docs live at docs dot fixture.") == ("portal", None)


def test_email_me_at_phrasings_are_detected():
    assert _d("If you're interested, email me at ada+hn@fixture.example") == ("email", "ada+hn@fixture.example")
    assert _d("Or, email me with a resume at ada@fixture.example, and I'll reply") == ("email", "ada@fixture.example")
    assert _d("Apply with what you've built: ada@fixture.example") == ("email", "ada@fixture.example")
