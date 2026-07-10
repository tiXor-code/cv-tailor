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
