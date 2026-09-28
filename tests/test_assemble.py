

def test_unfinished_projects_are_dropped_unless_nothing_else_was_chosen():
    """Teodor, 2026-09-28: half-built projects read as hobby work on the CV."""
    from cv_tailor.assemble import drop_unfinished_projects
    profile = {"projects": [
        {"id": "done", "tagline": "Live service", "bullets": ["Shipped."]},
        {"id": "wip", "tagline": "Fixture", "bullets": ["Architecture complete; rollout in progress."]},
        {"id": "dev", "tagline": "Fixture", "bullets": ["Active development; partial implementation shipping."]},
    ]}
    assert drop_unfinished_projects(profile, ["wip", "done", "dev"]) == ["done"]
    assert drop_unfinished_projects(profile, ["wip", "dev"]) == ["wip"]
    assert drop_unfinished_projects(profile, []) == []
