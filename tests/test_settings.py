"""The settings layer: settings.yaml on top of factory.yaml, and a project's overrides block."""
import pytest
import yaml

from factory import config, settings


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    path = tmp_path / "settings.yaml"
    monkeypatch.setenv("FACTORY_SETTINGS", str(path))
    return path


def test_defaults_pass_their_own_check():
    assert config.problems(config.load_defaults()) == []


def test_save_writes_only_what_differs_and_load_applies_it(overlay):
    defaults = config.load_defaults()
    result = settings.save_global({"pipeline": {"modes": {"review": "gate"}, "max_plan_rounds": defaults["pipeline"]["max_plan_rounds"]},
                                   "delivery": {"auto_merge": True}})
    assert result["ok"]
    assert yaml.safe_load(overlay.read_text()) == {"pipeline": {"modes": {"review": "gate"}}, "delivery": {"auto_merge": True}}
    cfg = config.load_config()
    assert cfg["pipeline"]["modes"] == {"plan_check": "advisory", "review": "gate", "judge": "advisory"}
    assert cfg["delivery"]["auto_merge"] is True

    # back to the default: the setting disappears from the file again
    assert settings.save_global({"pipeline": {"modes": {"review": "advisory"}}, "delivery": {"auto_merge": False}})["ok"]
    assert yaml.safe_load(overlay.read_text()) is None


def test_a_wrong_value_is_refused_and_nothing_is_written(overlay):
    result = settings.save_global({"limits": {"budget_usd_per_ticket": 0}, "roles": {"planner": {"effort": "extreme"}}})
    assert not result["ok"]
    assert any(line.startswith("limits.budget_usd_per_ticket") for line in result["problems"])
    assert any(line.startswith("roles.planner.effort") for line in result["problems"])
    assert not overlay.exists()


def test_a_job_must_name_a_known_capability(overlay):
    result = settings.save_global({"runtime": {"jobs": {"work": "mainframe"}}})
    assert not result["ok"] and "runtime.jobs.work" in result["problems"][0]


def test_raw_text_is_checked_too(overlay):
    assert settings.save_global(raw="verify:\n  satisfaction_threshold: 0.8  # strenger wäre 1.0\n")["ok"]
    assert "# strenger" in overlay.read_text()   # the person's own comments survive
    assert config.load_config()["verify"]["satisfaction_threshold"] == 0.8
    with pytest.raises(settings.SettingsError):
        settings.save_global(raw="verify: [unclosed")
    assert not settings.save_global(raw="verify:\n  satisfaction_threshold: 2\n")["ok"]


def test_a_broken_settings_file_stops_a_run_with_a_message(overlay):
    overlay.write_text("pipeline:\n  modes:\n    judge: sometimes\n")
    with pytest.raises(SystemExit) as stop:
        config.load_config()
    assert "pipeline.modes.judge" in str(stop.value)


def test_settings_can_be_switched_off(overlay, monkeypatch):
    overlay.write_text("delivery:\n  auto_merge: true\n")
    monkeypatch.setenv("FACTORY_SETTINGS", "off")
    assert config.load_config()["delivery"]["auto_merge"] is False


PROFILE = """# Profil
schema: profile/1
name: tiny
checks:
  - name: readme
    run: "test -f README.md"

# Dark, weil es ein Testprojekt ist.
overrides:
  pipeline:
    modes: { plan_check: gate }

# Danach kommt noch etwas.
env:
  A: "1"
"""


def test_overrides_block_is_replaced_and_the_rest_stays():
    new = settings.set_overrides(PROFILE, {"delivery": {"auto_merge": True}})
    assert yaml.safe_load(new)["overrides"] == {"delivery": {"auto_merge": True}}
    assert yaml.safe_load(new)["env"] == {"A": "1"}
    for comment in ("# Profil", "# Dark, weil es ein Testprojekt ist.", "# Danach kommt noch etwas."):
        assert comment in new
    assert new.replace("overrides:\n  delivery:\n    auto_merge: true\n", "X") != new


def test_overrides_block_is_added_and_removed():
    bare = "schema: profile/1\nname: tiny\nchecks:\n  - name: a\n    run: 'true'\n"
    added = settings.set_overrides(bare, {"limits": {"budget_usd_per_ticket": 5}})
    assert added.startswith(bare) and yaml.safe_load(added)["overrides"] == {"limits": {"budget_usd_per_ticket": 5}}
    assert settings.set_overrides(added, {}) == bare
    removed = settings.set_overrides(PROFILE, {})
    assert "overrides" not in yaml.safe_load(removed) and yaml.safe_load(removed)["env"] == {"A": "1"}


def test_a_wrong_project_override_stops_the_run(product):
    from factory import cli
    profile = product.repo / ".factory" / "profile.yaml"
    profile.write_text(profile.read_text() + "overrides:\n  pipeline:\n    modes: { review: maybe }\n")
    import subprocess
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "override"],
                   cwd=product.repo, check=True)
    with pytest.raises(SystemExit) as stop:
        cli.main(product.args)
    assert "pipeline.modes.review" in str(stop.value)


def test_a_prompt_changed_on_the_page_is_what_the_station_gets(overlay):
    from factory import stations
    defaults = settings.state([])["defaults"]
    original = defaults["prompts"]["stations/intake.md"]
    assert original == (config.ROOT / "stations/intake.md").read_text()
    changed = original + "\nAlways answer in short sentences.\n"
    assert settings.save_global({"prompts": {"stations/intake.md": changed}})["ok"]
    saved = yaml.safe_load(overlay.read_text())
    assert list(saved["prompts"]) == ["stations/intake.md"]          # only the changed prompt is written
    cfg = config.load_config()
    assert stations.prompt_text("stations/intake.md", cfg.get("prompts")) == changed
    assert stations.prompt_text("stations/planner.md", cfg.get("prompts")) == (config.ROOT / "stations/planner.md").read_text()
    # set back to the file's text: no override left
    assert settings.save_global({"prompts": {"stations/intake.md": original}})["ok"]
    assert yaml.safe_load(overlay.read_text()) is None


def test_a_prompt_may_only_use_the_placeholders_of_its_file(overlay):
    result = settings.save_global({"prompts": {"stations/intake.md": "Read {{no_such_value}} and stop."}})
    assert not result["ok"] and "no_such_value" in " ".join(result["problems"]) and not overlay.exists()


def test_the_board_project_list_stays_on_this_machine(overlay):
    """A published factory must not carry its owner's projects: board.repos, board.local and machine go to
    settings.local.yaml, which git ignores; the rest stays in settings.yaml that GitHub runs read."""
    local = overlay.with_name("settings.local.yaml")
    assert settings.save_global({"board": {"repos": ["me/a", "me/b"], "show_cost": True},
                                 "machine": {"orphan_minutes": 7}, "delivery": {"auto_merge": True}})["ok"]
    assert yaml.safe_load(overlay.read_text()) == {"board": {"show_cost": True}, "delivery": {"auto_merge": True}}
    assert yaml.safe_load(local.read_text()) == {"board": {"repos": ["me/a", "me/b"]}, "machine": {"orphan_minutes": 7}}
    cfg = config.load_config()
    assert cfg["board"]["repos"] == ["me/a", "me/b"] and cfg["board"]["show_cost"] is True

    # the text editor shows settings.yaml only; saving it keeps this machine's settings
    assert settings.save_global(raw="delivery:\n  auto_merge: false\n")["ok"]
    assert config.load_config()["board"]["repos"] == ["me/a", "me/b"]
    # a list written into the text moves out of it again, without losing the other local values
    assert settings.save_global(raw="board:\n  repos: [me/c]\n")["ok"]
    assert "repos" not in overlay.read_text()
    assert yaml.safe_load(local.read_text()) == {"board": {"repos": ["me/c"]}, "machine": {"orphan_minutes": 7}}


def test_an_old_settings_yaml_with_the_project_list_still_works(overlay):
    overlay.write_text("board:\n  repos: [me/old]\n")
    assert config.load_config()["board"]["repos"] == ["me/old"]
