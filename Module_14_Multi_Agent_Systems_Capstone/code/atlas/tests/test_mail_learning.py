"""core.mail_learning: people, noise and open loops learned from mail metadata."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import yaml

from core import context_pack as cp
from core import mail_learning as ml
from core.mail_learning import Message, Thread, analyze

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)
ME = "Trilok Kalani <person3@gmail.com>"


def _t(tid, *msgs):
    return Thread(tid, [Message(s, to, "", subj, NOW - timedelta(days=d)) for s, to, subj, d in msgs])


def test_ranks_back_and_forth_and_finds_open_loops():
    threads = [
        _t("a", (ME, "Pat Lee <person1@gmail.com>", "Shelf count", 10),
               ("Pat Lee <person1@gmail.com>", ME, "Re: Shelf count", 9),
               (ME, "Pat Lee <person1@gmail.com>", "Re: Shelf count", 5)),
        _t("b", (ME, "Chris <mike@example.com>", "Website feedback", 2)),
        _t("c", ("Brenda <person9@pg.example>", ME, "Credit on report", 1),),
        _t("d", (ME, "Trilok Kalani <person10@yahoo.com>", "note to self", 3),
               (ME, "Trilok Kalani <person10@yahoo.com>", "note to self 2", 3)),
        _t("e", (ME, "Old Pal <old@example.com>", "hi", 120), ("Old Pal <old@example.com>", ME, "hey", 119)),
    ]
    out = analyze("person3@gmail.com", threads, [], now=NOW)
    people = {p["emails"][0]: p for p in out["people"]}
    assert people["person1@gmail.com"]["tier"] == 1
    assert people["person1@gmail.com"]["name"] == "Pat Lee"
    assert people["old@example.com"]["tier"] == 2  # back-and-forth but not recent
    assert "person10@yahoo.com" not in people  # his own other address, by display name
    assert "mike@example.com" not in people  # one message, no reply: not yet a correspondent
    loops = {(x["owner"], x["addr"]) for x in out["open_loops_raw"]}
    assert ("them", "person1@gmail.com") in loops  # he sent last, 5 days ago
    assert ("me", "person9@pg.example") in loops  # she wrote last, a day ago
    assert not any(x["addr"] == "mike@example.com" for x in out["open_loops_raw"])  # only 2 days


def test_noise_is_never_opened_and_never_written_to():
    threads = [_t("a", (ME, "Friend <person11@gmail.com>", "hi", 3), ("Friend <person11@gmail.com>", ME, "yo", 2))]
    unread = (["Promo Mailer <news@ak.promo-mailer.example>"] * 5 + ["Friend <person11@gmail.com>"] * 5
              + ["Spammer <person12@gmail.com>"] * 4 + ["Rare <rare@shop.com>"] * 2)
    noise = analyze("person3@gmail.com", threads, unread, now=NOW)["noise"]
    assert "ak.promo-mailer.example" in noise["domains"]
    assert "person12@gmail.com" in noise["addresses"] and "gmail.com" not in noise["domains"]
    assert "person11@gmail.com" not in noise["addresses"]
    assert "shop.com" not in noise["domains"]  # seen twice: not enough


def test_merge_hand_written_wins_and_learned_fills_in():
    manual = {"snapshot": "2026-10-08",
              "people": [{"name": "Dr. Pat Lee", "emails": ["person1@gmail.com"], "tier": 1,
                          "project": "demo-dental"}],
              "noise": {"domains": ["promo-mailer.example"]}, "rules": ["r"],
              "open_loops": {"demo-dental": [{"owner": "them", "text": "old"}]}}
    learned = {"people": [{"name": "Will", "emails": ["person1@gmail.com"], "tier": 2},
                          {"name": "New Person", "emails": ["new@x.com"], "tier": 1}],
               "noise": {"domains": ["shein.com"]},
               "open_loops_at": "2026-10-09T06:40:00+00:00",
               "open_loops": {"inbox": [{"owner": "me", "text": "fresh"}]}}
    m = cp.merge(manual, learned)
    names = [p["name"] for p in m["people"]]
    assert names == ["Dr. Pat Lee", "New Person"]
    assert set(m["noise"]["domains"]) == {"promo-mailer.example", "shein.com"}
    # Open loops are a union: a learned refresh adds, it never erases a hand-written loop.
    assert m["open_loops"] == {"demo-dental": [{"owner": "them", "text": "old"}],
                               "inbox": [{"owner": "me", "text": "fresh"}]}
    assert m["snapshot"] == "2026-10-09"
    older = {**learned, "open_loops_at": "2026-10-01T00:00:00+00:00"}
    assert cp.merge(manual, older)["snapshot"] == "2026-10-08"


class _FakeGmail:
    """Just enough of the Gmail API for learn() and refresh_open_loops()."""

    def __init__(self):
        ts = int((NOW - timedelta(days=4)).timestamp() * 1000)
        self.threads = {
            "t1": [("Trilok Kalani <person3@gmail.com>", "Pat Lee <person1@gmail.com>", "Shelf count", ts - 10**8),
                   ("Pat Lee <person1@gmail.com>", "person3@gmail.com", "Re: Shelf count", ts - 10**7),
                   ("Trilok Kalani <person3@gmail.com>", "Pat Lee <person1@gmail.com>", "Re: Shelf count", ts)],
        }
        self.unread = {f"u{i}": "Promo <deals@store.example>" for i in range(4)}

    async def _get(self, path, params=None):
        def msg(frm, to, subj, ts):
            return {"internalDate": str(ts), "labelIds": [], "payload": {"headers": [
                {"name": "From", "value": frm}, {"name": "To", "value": to}, {"name": "Subject", "value": subj}]}}
        if path == "/profile":
            return {"emailAddress": "person3@gmail.com"}
        if path == "/threads":
            return {"threads": [{"id": k} for k in self.threads]}
        if path.startswith("/threads/"):
            return {"messages": [msg(*m) for m in self.threads[path.split("/")[-1]]]}
        if path == "/messages":
            return {"messages": [{"id": k} for k in self.unread]}
        if path.startswith("/messages/"):
            return msg(self.unread[path.split("/")[-1]], "", "", 0)
        raise AssertionError(path)


async def test_learn_writes_the_learned_file_and_load_merges_it(tmp_path, monkeypatch):
    learned_path = tmp_path / "context.learned.yaml"
    monkeypatch.setattr(ml, "LEARNED_PATH", learned_path)
    monkeypatch.setattr(cp, "LEARNED_PATH", learned_path)
    summary = await ml.learn(_FakeGmail(), label=False)
    assert summary["people"] == 1 and summary["noise_domains"] == 1
    data = yaml.safe_load(learned_path.read_text(encoding="utf-8"))
    assert data["people"][0]["emails"] == ["person1@gmail.com"]
    assert data["noise"]["domains"] == ["store.example"]
    assert data["open_loops"]["demo-dental"][0]["owner"] == "them"  # mapped via config/context.yaml
    assert ml.learned_age_days() < 0.01
    merged = cp.load()
    assert "store.example" in merged["noise"]["domains"]

    summary = await ml.refresh_open_loops(_FakeGmail())
    assert summary["open_loops"] == 1
    assert yaml.safe_load(learned_path.read_text(encoding="utf-8"))["people"]  # people kept


async def test_scheduled_jobs_are_quiet_without_gmail(monkeypatch):
    monkeypatch.setattr(ml, "gmail_configured", lambda: False)
    called = []
    async def boom(*a, **k):
        called.append(1)
    monkeypatch.setattr(ml, "learn", boom)
    monkeypatch.setattr(ml, "refresh_open_loops", boom)
    await ml.scheduled_learn()
    await ml.scheduled_refresh()
    assert not called
