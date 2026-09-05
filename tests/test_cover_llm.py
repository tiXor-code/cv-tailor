from types import SimpleNamespace

from cv_tailor.cover_llm import build_messages, check_cover, cover_letter


def test_check_cover_flags_slop_and_shape():
    good = " ".join(["word"] * 140)
    assert check_cover(good) == []
    assert any("banned" in w for w in check_cover("I am excited to " + good))
    assert any("dash" in w for w in check_cover("A note — with a dash. " + good))
    assert any("too short" in w for w in check_cover("three words only"))
    assert any("too long" in w for w in check_cover(" ".join(["word"] * 300)))


class _FakeClient:
    """Returns queued responses; records how many times it was called."""
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls += 1
        text = self._replies.pop(0)
        msg = SimpleNamespace(content=text)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


_PROFILE = {"skills": {"core": ["Python"]}, "summary_pool": [{"id": "s1", "text": "Builder."}]}
_FIELDS = {"job_meta": {"company": "Acme", "role": "AI Engineer"},
           "one_line_pitch": "Ships agents.", "jd_keywords_matched": ["python"],
           "skills_emphasis": ["Python"], "experience_ids_ordered": ["e1"],
           "project_ids": ["p1"], "gaps_honest": []}


def test_cover_letter_retries_on_slop_then_returns_clean():
    clean = " ".join(["shipped"] * 140)
    client = _FakeClient(["I am excited to apply. " + clean, clean])
    out = cover_letter(_PROFILE, "JD text", _FIELDS, client=client)
    assert out == clean
    assert client.calls == 2  # first draft tripped the guard, retried once


def test_cover_letter_no_retry_when_first_is_clean():
    clean = " ".join(["delivered"] * 140)
    client = _FakeClient([clean, "SHOULD-NOT-BE-USED"])
    out = cover_letter(_PROFILE, "JD text", _FIELDS, client=client)
    assert out == clean
    assert client.calls == 1


def test_cover_letter_stops_after_max_attempts_and_returns_fewest_warnings():
    """Never loop forever. Return the LEAST-bad draft, not merely the last one."""
    body = " ".join(["word"] * 140)
    two_bad = "I am excited to leverage this. " + body       # 2 banned phrases
    one_bad = "I am excited about this. " + body             # 1 banned phrase
    client = _FakeClient([two_bad, one_bad, two_bad])
    out = cover_letter(_PROFILE, "JD text", _FIELDS, client=client)
    assert out == one_bad.strip()          # fewest warnings wins, though it came 2nd
    assert client.calls == 3               # bounded by MAX_ATTEMPTS
    assert check_cover(out)                # caller records residual warnings in meta.json


def test_retry_feeds_previous_draft_back_for_revision():
    clean = " ".join(["shipped"] * 140)
    bad = "I am excited. " + clean
    client = _FakeClient([bad, clean])
    captured = []
    orig = client._create

    def spy(**kw):
        captured.append(kw["messages"][-1]["content"])
        return orig(**kw)

    client.chat.completions.create = spy
    cover_letter(_PROFILE, "JD text", _FIELDS, client=client)
    retry = captured[1]
    assert "Problems to fix" in retry
    assert "banned phrase" in retry
    assert "Revise the draft below" in retry      # revise, do not regenerate
    assert bad in retry                            # the model sees its own draft


def test_pitch_is_marked_do_not_reuse_and_gaps_reach_model():
    # the CV step's pitch often carries cliches; it must not prime the letter
    fields = {**_FIELDS, "gaps_honest": ["no Kubernetes in production"],
              "one_line_pitch": "Passionate engineer with a proven track record."}
    user = build_messages(_PROFILE, "SOME JD BODY", fields)[1]["content"]
    assert "SOME JD BODY" in user
    assert "no Kubernetes in production" in user   # gaps must not be papered over
    assert "DO NOT reuse its wording" in user
