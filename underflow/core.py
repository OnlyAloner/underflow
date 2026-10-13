"""Что делает база, без HTTP: искать, принимать, голосовать, садовничать."""

import html
import json
import logging
import time
from collections import OrderedDict

from .config import Config
from .db import DB
from .librarian import Librarian, LibrarianError
from .scrub import scrub, scrub_many

log = logging.getLogger("underflow")

RANK_POOL = 12      # сколько кандидатов полнотекста показывать библиотекарю
MIN_RELEVANCE = 0.35


class Rejected(Exception):
    pass


class Underflow:
    def __init__(self, cfg: Config, db: DB | None = None, librarian: Librarian | None = None):
        self.cfg = cfg
        self.db = db or DB(cfg.db_path)
        self.lib = librarian or Librarian(cfg)
        self._cache: OrderedDict[str, tuple[float, list[dict]]] = OrderedDict()

    # --- поиск ----------------------------------------------------------

    def search(self, query: str, context: dict | None = None, k: int = 5) -> dict:
        context = context or {}
        query, _ = scrub(query.strip())
        key = json.dumps([query.lower(), context], sort_keys=True, ensure_ascii=False)
        notes: list[str] = []

        ranked = self._cached(key)
        if ranked is None:
            ranked = self._rank(query, context, notes)
            self._cache[key] = (time.time(), ranked)
            while len(self._cache) > 500:
                self._cache.popitem(last=False)
        else:
            notes.append("cached")

        results = []
        for r in ranked[:k]:
            q = self.db.question(r["id"], answers_limit=3)
            if q:
                results.append({**q, "relevance": r.get("relevance"), "why": r.get("why", "")})
        sid = self.db.log_search(query, context, [r["id"] for r in results])
        return {"search_id": sid, "librarian": self.lib.mode, "notes": notes, "results": results}

    def _cached(self, key: str):
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        return None

    def _rank(self, query: str, context: dict, notes: list[str]) -> list[dict]:
        text = " ".join([query, context.get("error", ""), " ".join(context.get("tools", []) or [])])
        found = dict(self.db.fts(text, 20))

        if not found and self.lib.enabled:
            try:
                for alt in self.lib.expand(query, context):
                    for qid, score in self.db.fts(alt, 10):
                        found[qid] = max(found.get(qid, 0), score)
                notes.append("expanded")
            except LibrarianError as e:
                notes.append(f"expand failed: {e}")

        by_fts = sorted(found.items(), key=lambda x: x[1], reverse=True)
        if not by_fts:
            return []
        if not self.lib.enabled:
            return [{"id": qid, "relevance": None, "why": "full-text match"} for qid, _ in by_fts]

        pool = [self.db.question(qid, answers_limit=1) for qid, _ in by_fts[:RANK_POOL]]
        try:
            ranked = self.lib.rank(query, context, [p for p in pool if p])
        except LibrarianError as e:
            notes.append(f"rank failed, full-text order: {e}")
            return [{"id": qid, "relevance": None, "why": "full-text match"} for qid, _ in by_fts]
        return [r for r in ranked if (r.get("relevance") or 0) >= MIN_RELEVANCE]

    # --- приём ----------------------------------------------------------

    def post(self, title: str, problem: str, answer: str, context: dict | None = None,
             tags: list[str] | None = None, search_id: str | None = None,
             question_id: str | None = None, author: str = "") -> dict:
        """Новый вопрос с ответом или ответ к существующему (question_id)."""
        context = dict(context or {})
        tags = [t.strip().lower() for t in (tags or []) if t.strip()][:8]
        (title, problem, answer), redacted = scrub_many(title, problem, answer)
        if not answer.strip():
            raise Rejected("answer is empty")

        target = self.db.question(question_id, answers_limit=1) if question_id else None
        if question_id and not target:
            raise KeyError(question_id)
        if target and not title:
            title = target["title"]

        curated = None
        if self.lib.enabled:
            cands = [target] if target else self._dupe_candidates(title, problem)
            try:
                curated = self.lib.curate(title, problem, answer, context, cands)
            except LibrarianError as e:
                log.warning("curate failed, storing raw: %s", e)
        if curated and curated["verdict"] == "reject":
            raise Rejected(curated.get("reject_reason") or "rejected by librarian")

        context["curated"] = bool(curated)
        if curated:
            # и то, что вернула модель, проходит через регулярки ещё раз
            (c_title, c_problem, c_answer), more = scrub_many(
                *(html.unescape(curated[k]) for k in ("title", "problem", "answer")))
            redacted += [k for k in more if k not in redacted]
            aliases = [scrub(a)[0] for a in curated.get("aliases", [])]
            if title and title != c_title:
                aliases.append(title)
            title, problem, answer = c_title, c_problem, c_answer
            tags = sorted(set(tags) | {t.lower() for t in curated.get("tags", [])})[:8]
            dup = target["id"] if target else curated.get("duplicate_of") or ""
        else:
            aliases, dup = [], target["id"] if target else ""

        if dup:
            qid, status = dup, "merged"
            self.db.update_question(qid, tags=tags)
        else:
            if not title:
                raise Rejected("title is empty")
            qid, status = self.db.add_question(title, problem, context, tags), "created"
        aid = self.db.add_answer(qid, answer, context, author=author)
        self.db.add_aliases(qid, aliases, "phrasing")

        if search_id:
            s = self.db.search_row(search_id)
            if s:
                self.db.add_aliases(qid, [s["query"]], "miss")
                self.db.resolve_search(search_id, qid)

        self._cache.clear()
        return {"status": status, "question_id": qid, "answer_id": aid,
                "title": title, "redacted": redacted, "curated": bool(curated)}

    def _dupe_candidates(self, title: str, problem: str) -> list[dict]:
        ids = [qid for qid, _ in self.db.fts(f"{title} {problem}", 8)]
        return [q for q in (self.db.question(i, answers_limit=1) for i in ids) if q]

    # --- голос ----------------------------------------------------------

    def vote(self, answer_id: str, worked: bool, note: str = "", search_id: str | None = None) -> dict:
        note, _ = scrub(note)
        if not self.db.vote(answer_id, worked, note[:500], search_id):
            raise KeyError(answer_id)
        row = self.db.conn.execute(
            "select question_id, worked, failed from answers where id=?", (answer_id,)).fetchone()
        if search_id and worked:
            s = self.db.search_row(search_id)
            if s:
                # запрос, по которому ответ нашли и он сработал, это проверенная формулировка
                self.db.add_aliases(row["question_id"], [s["query"]], "hit")
                self.db.resolve_search(search_id, row["question_id"])
        self._cache.clear()
        return {"answer_id": answer_id, "worked": row["worked"], "failed": row["failed"]}

    # --- садовник -------------------------------------------------------

    def garden(self, limit: int = 30) -> dict:
        """Промахи -> псевдонимы, сырые записи -> через библиотекаря."""
        done = {"misses_seen": 0, "misses_linked": 0, "curated": 0, "removed": 0}
        if not self.lib.enabled:
            return done

        for s in self.db.misses(limit=limit):
            done["misses_seen"] += 1
            ctx = json.loads(s["context"] or "{}")
            ids = [qid for qid, _ in self.db.fts(s["query"], 8)]
            try:
                if not ids:
                    for alt in self.lib.expand(s["query"], ctx):
                        ids += [qid for qid, _ in self.db.fts(alt, 5) if qid not in ids]
                cands = [q for q in (self.db.question(i, 1) for i in ids[:RANK_POOL]) if q]
                qid = self.lib.garden(s["query"], ctx, cands) if cands else ""
            except LibrarianError as e:
                log.warning("garden: %s", e)
                break
            if qid:
                self.db.add_aliases(qid, [s["query"]], "miss")
                self.db.resolve_search(s["id"], qid)
                done["misses_linked"] += 1
            self.db.mark_gardened(s["id"])

        raw = self.db.conn.execute(
            "select a.id as aid, a.question_id as qid, a.body, a.context from answers a "
            "where json_extract(a.context, '$.curated') = 0 limit ?", (limit,)).fetchall()
        for r in raw:
            q = self.db.question(r["qid"], answers_limit=0)
            try:
                cur = self.lib.curate(q["title"], q["body"], r["body"], q["context"], [])
            except LibrarianError as e:
                log.warning("garden curate: %s", e)
                break
            self._apply_curation(q, r, cur, done)

        self._cache.clear()
        return done

    def _apply_curation(self, q: dict, r, cur: dict, done: dict):
        conn, lock = self.db.conn, self.db.lock
        with lock:
            if cur["verdict"] == "reject":
                conn.execute("delete from votes where answer_id=?", (r["aid"],))
                conn.execute("delete from answers where id=?", (r["aid"],))
                left = conn.execute("select count(*) from answers where question_id=?", (q["id"],)).fetchone()[0]
                if not left:
                    conn.execute("delete from aliases where question_id=?", (q["id"],))
                    conn.execute("delete from qfts where qid=?", (q["id"],))
                    conn.execute("delete from questions where id=?", (q["id"],))
                else:
                    self.db.reindex(q["id"])
                done["removed"] += 1
                return
            (title, problem, answer), _ = scrub_many(
                *(html.unescape(cur[k]) for k in ("title", "problem", "answer")))
            ctx = json.loads(r["context"] or "{}")
            ctx["curated"] = True
            conn.execute("update answers set body=?, context=? where id=?",
                         (answer, json.dumps(ctx, ensure_ascii=False), r["aid"]))
            if len(q["answers"] if q.get("answers") else []) <= 1:
                conn.execute("update questions set title=?, body=? where id=?", (title, problem, q["id"]))
        self.db.update_question(q["id"], tags=[t.lower() for t in cur.get("tags", [])])
        self.db.add_aliases(q["id"], [scrub(a)[0] for a in cur.get("aliases", [])] + [q["title"]], "phrasing")
        done["curated"] += 1
