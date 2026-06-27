"""Phase C tests: AI Organization rules in UserConfig (parse / clean / persist)."""

from __future__ import annotations

from pathlib import Path

from app.config.user_config import OrgRule, UserConfig, UserConfigStore


def test_org_rules_normalized_cleans_and_coerces():
    c = UserConfig(organization_rules=[
        {"match": "DOMAIN", "value": " Google.com ", "label": " Google "},
        {"match": "weird", "value": "x", "label": "L"},          # bad match -> default "domain"
        {"match": "subject", "value": "", "label": "L"},          # dropped (no value)
        {"match": "sender", "value": "amazon", "label": ""},      # dropped (no label)
    ]).normalized()
    rules = c.organization_rules
    assert len(rules) == 2
    assert rules[0].match == "domain" and rules[0].value == "google.com" and rules[0].label == "Google"
    assert rules[1].match == "domain"  # "weird" coerced


def test_org_rules_round_trip(tmp_path: Path):
    store = UserConfigStore(tmp_path / "uc.json")
    store.save(UserConfig(organization_rules=[OrgRule(match="sender", value="github", label="Dev")]))
    loaded = store.load()
    assert len(loaded.organization_rules) == 1
    assert loaded.organization_rules[0].match == "sender"
    assert loaded.organization_rules[0].value == "github"
    assert loaded.organization_rules[0].label == "Dev"


def test_default_config_has_no_org_rules():
    assert UserConfig().normalized().organization_rules == []
