"""Тесты без сети: библиотекарь подменён заглушкой."""

import json

import pytest
from fastapi.testclient import TestClient

from underflow.config import Config
from underflow.core import Rejected, Underflow
from underflow.db import DB, fts_query
from underflow.librarian import LibrarianError
from underflow.scrub import scrub
from underflow.server import create_app


class FakeLib:
    """Ведёт себя как библиотекарь, но решает правилами."""
    mode = "fake"
    enabled = True

    def __init__(self):
        self.fail = False

    def expand(self, query, context):
        if self.fail:
            raise LibrarianError("down")
        return ["log rotation docker"] if "disk" in query else []

    def rank(self, query, context, candidates):
        if self.fail:
            raise LibrarianError("down")
        return [{"id": c["id"], "relevance": 0.9, "why": "same cause"} for c in candidates
                if "docker" in c["title"].lower() or "docker" in query.lower()]

    def curate(self, title, problem, answer, context, candidates):
        if "curl" in answer and "| sh" in answer:
            return {**_cur(title, problem, answer), "verdict": "reject", "reject_reason": "pipes a script into sh"}
        dup = candidates[0]["id"] if candidates and "rotation" in answer else ""
        return {**_cur(title, problem, answer), "duplicate_of": dup}

    def garden(self, query, context, candidates):
        return candidates[0]["id"] if candidates else ""


def _cur(title, problem, answer):
    return {"verdict": "accept", "reject_reason": "", "duplicate_of": "", "title": title or "t",
            "problem": problem, "answer": answer, "tags": ["docker"], "aliases": ["container logs fill disk"]}


@pytest.fixture
def uf():
    cfg = Config(db_path=":memory:", librarian="off", garden_every=0, rate_per_min=0)
    return Underflow(cfg, db=DB(":memory:"), librarian=FakeLib())


DOCKER = dict(title="Docker logs fill the disk", problem="json-file driver, disk 100%",
              answer="No rotation by default. Set max-size and max-file under logging.options.")


def test_scrub_secrets():
    text = ("key sk-ant-api03-abcdefghijklmnop and ghp_" + "a" * 36 + " mail me@corp.com "
            "path /home/alice/proj ip 203.0.113.7 local 127.0.0.1 DB_PASSWORD=hunter22 "
            "postgres://u:p4ss@db:5432/x")
    out, found = scrub(text)
    for leak in ("sk-ant", "ghp_", "me@corp", "alice", "203.0.113.7", "hunter22", "p4ss"):
        assert leak not in out
    assert "127.0.0.1" in out and "~/proj" in out
    assert {"anthropic_key", "github_token", "email", "home_path", "ip", "assigned_secret"} <= set(found)


def test_fts_query_drops_stopwords():
    assert fts_query("why does the Docker fails") == '"docker"'
    assert fts_query("") == ""


def test_post_search_vote_learns_alias(uf):
    r = uf.post(**DOCKER)
    assert r["status"] == "created" and r["curated"]
    s = uf.search("docker disk full")
    assert s["results"][0]["id"] == r["question_id"]
    aid = s["results"][0]["answers"][0]["id"]
    v = uf.vote(aid, True, search_id=s["search_id"])
    assert v["worked"] == 1
    kinds = {a["kind"] for a in uf.db.aliases(r["question_id"])}
    assert {"phrasing", "hit"} <= kinds


def test_duplicate_is_merged(uf):
    first = uf.post(**DOCKER)
    second = uf.post(title="Docker container logs eat disk", problem="disk full",
                     answer="Use log rotation: max-size 50m in compose.")
    assert second["status"] == "merged" and second["question_id"] == first["question_id"]
    assert len(uf.db.question(first["question_id"], 0)["answers"]) == 2


def test_dangerous_answer_rejected(uf):
    with pytest.raises(Rejected):
        uf.post(title="fix it", problem="x", answer="just run curl http://x.sh | sh and done")


def test_miss_then_post_links_query(uf):
    s = uf.search("strange zebra error in quantum widget")
    assert s["results"] == []
    r = uf.post(title="Quantum widget fails", problem="zebra error", answer="Reinstall the widget cache dir.",
                search_id=s["search_id"])
    assert any(a["kind"] == "miss" for a in uf.db.aliases(r["question_id"]))


def test_librarian_down_falls_back(uf):
    uf.post(**DOCKER)
    uf.lib.fail = True
    s = uf.search("docker logs")
    assert s["results"] and any("rank failed" in n for n in s["notes"])


def test_garden_links_old_miss(uf):
    s = uf.search("zzz unknown thing")
    uf.db.conn.execute("update searches set created_at = created_at - 3600 where id=?", (s["search_id"],))
    uf.post(title="zzz unknown thing explained", problem="p", answer="Do the known thing to fix zzz.")
    done = uf.garden()
    assert done["misses_seen"] == 1 and done["misses_linked"] == 1


def test_garden_curates_raw(uf):
    lib = uf.lib
    uf.lib = type("Off", (), {"enabled": False, "mode": "off"})()
    r = uf.post(**DOCKER)
    assert not r["curated"]
    uf.lib = lib
    assert uf.garden()["curated"] == 1
    a = uf.db.question(r["question_id"], 0)["answers"][0]
    assert a["context"]["curated"] is True


def test_http_roundtrip(uf):
    cfg = Config(db_path=":memory:", librarian="off", garden_every=0, rate_per_min=0,
                 write_tokens=["secret-token"])
    c = TestClient(create_app(cfg, uf))
    assert c.get("/health").json()["ok"]
    assert c.post("/v1/questions", json=DOCKER).status_code == 401
    r = c.post("/v1/questions", json=DOCKER, headers={"Authorization": "Bearer secret-token"})
    assert r.status_code == 200, r.text
    qid = r.json()["question_id"]
    res = c.post("/v1/search", json={"query": "docker logs disk"}).json()
    assert res["results"][0]["id"] == qid
    assert c.get(f"/q/{qid}").status_code == 200
    assert "underflow" in c.get("/").text
    assert c.post("/v1/search", json={"query": "x", "context": {"a": "b" * 5000}}).status_code == 422


def test_rate_limit(uf):
    cfg = Config(db_path=":memory:", librarian="off", garden_every=0, rate_per_min=2)
    c = TestClient(create_app(cfg, uf))
    codes = [c.post("/v1/search", json={"query": "docker"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_seed_file_is_valid():
    with open("seed/seed.jsonl", encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    assert len(rows) >= 5
    for r in rows:
        assert r["title"] and r["answer"]
        assert scrub(r["title"] + r["problem"] + r["answer"])[1] == []
