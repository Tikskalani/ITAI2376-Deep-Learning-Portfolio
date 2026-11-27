"""config/context.yaml: known people rank up, noise never becomes a signal."""

from __future__ import annotations

from core import context_pack as cp
from core.connectors.gmail import message_to_signal

DATA = {
    "snapshot": "2026-10-08",
    "people": [
        {"name": "Dr. Pat Lee", "emails": ["person1@gmail.com"], "project": "demo-dental", "tier": 1,
         "note": "Client."},
        {"name": "PracticeSoft", "domains": ["practicesoft.example"], "project": "demo-dental", "tier": 2},
    ],
    "noise": {"domains": ["promo-mailer.example", "listings@redfin.com"], "addresses": ["updates-noreply@linkedin.com"]},
    "rules": ["Mail from a tier 1 person is importance 3."],
    "open_loops": {"demo-dental": [{"owner": "them", "who": "Dr. Pat Lee", "since": "2026-09-28",
                                     "text": "Shelf count."}]},
}


def test_noise_by_domain_subdomain_and_address():
    assert cp.is_noise("Promo Mailer <news@ak.promo-mailer.example>", DATA)
    assert cp.is_noise("updates-noreply@linkedin.com", DATA)
    assert cp.is_noise("Redfin <listings@redfin.com>", DATA)
    assert not cp.is_noise("other@redfin.com", DATA)
    assert not cp.is_noise("Joe <joe@example.com>", DATA)


def test_known_people_match_by_address_then_domain_and_beat_noise():
    assert cp.sender_hint("Pat Lee <person1@gmail.com>", DATA)["tier"] == 1
    assert cp.sender_hint("person4@practicesoft.example", DATA)["project"] == "demo-dental"
    assert cp.sender_hint("person5@gmail.com", DATA) is None
    noisy_friend = {**DATA, "noise": {"domains": ["gmail.com"]}}
    assert not cp.is_noise("person1@gmail.com", noisy_friend)


def test_blocks_render():
    assert "Dr. Pat Lee [tier 1 | demo-dental]" in cp.people_block(DATA)
    loops = cp.open_loops_block(DATA)
    assert "as of 2026-10-08" in loops and "waiting on Dr. Pat Lee (since 2026-09-28)" in loops
    tri = cp.triage_block(DATA)
    assert "Known people" in tri and "importance 3" in tri


def test_missing_or_broken_file_degrades_to_empty(tmp_path):
    assert cp.load(tmp_path / "nope.yaml") == {}
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a list\n", encoding="utf-8")
    assert cp.load(bad) == {}


def test_repo_context_file_parses():
    data = cp.load(cp.PATH)
    assert data.get("people") and data.get("noise") and data.get("open_loops")
    slugs = set(data["open_loops"])
    from scripts.seed_projects import SEED
    assert slugs <= {row[0] for row in SEED}, "open_loops must use project slugs from the seed"


def _msg(sender: str) -> dict:
    return {"id": "m1", "threadId": "t1", "internalDate": "1791400000000", "labelIds": ["INBOX"],
            "snippet": "hi", "payload": {"headers": [{"name": "From", "value": sender},
                                                     {"name": "Subject", "value": "Hello"}]}}


def test_gmail_drops_noise_and_tags_known_senders(monkeypatch):
    monkeypatch.setattr(cp, "load", lambda path=None: DATA)
    assert message_to_signal(_msg("Promo Mailer <news@ak.promo-mailer.example>")) is None
    sig = message_to_signal(_msg("Pat Lee <person1@gmail.com>"))
    assert sig is not None and sig.raw["known"]["name"] == "Dr. Pat Lee"
    assert message_to_signal(_msg("x@example.com")).raw["known"] is None
