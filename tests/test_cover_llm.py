from types import SimpleNamespace

from cv_tailor.cover_llm import check_cover, cover_letter


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
