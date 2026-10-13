"""Хранилище: SQLite + FTS5. Один файл, бэкап = скопировать файл.

Вопрос держит рядом с собой «псевдонимы»: другие формулировки той же
проблемы, тексты ошибок и запросы, с которыми агенты сюда уже приходили.
Полнотекстовый поиск ищет по всем ним сразу, поэтому база со временем
находит вопрос по тем словам, какими его реально ищут.
"""

import json
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
create table if not exists questions(
  id text primary key,
  title text not null,
  body text not null default '',
  context text not null default '{}',
  tags text not null default '',
  created_at real not null,
  updated_at real not null,
  hits integer not null default 0
);
create table if not exists answers(
  id text primary key,
  question_id text not null references questions(id),
  body text not null,
  context text not null default '{}',
  created_at real not null,
  worked integer not null default 0,
  failed integer not null default 0,
  author text not null default ''
);
create index if not exists answers_q on answers(question_id);
create table if not exists aliases(
  question_id text not null references questions(id),
  text text not null,
  kind text not null,
  created_at real not null,
  unique(question_id, text)
);
create table if not exists searches(
  id text primary key,
  query text not null,
  context text not null default '{}',
  result_ids text not null default '[]',
  created_at real not null,
  resolved_question_id text,
  gardened integer not null default 0
);
create table if not exists votes(
  answer_id text not null references answers(id),
  worked integer not null,
  note text not null default '',
  created_at real not null,
  search_id text
);
create virtual table if not exists qfts using fts5(
  qid unindexed, title, body, aliases, answers, tags,
  tokenize = 'unicode61 remove_diacritics 2'
);
"""

# веса колонок для bm25: qid, title, body, aliases, answers, tags
_BM25 = "bm25(qfts, 0.0, 6.0, 1.5, 4.0, 1.0, 2.0)"

_STOP = set("""
a an the to of in on for and or is are was be been it this that with as at by from how what why
when do does did not no can cant cannot i my me we you your error fails failed failing get got
и в во на не что как почему при с со по из у к а но или это то же ли для от до за
""".split())

_WORD = re.compile(r"[\w][\w.\-]*[\w]|\w", re.U)


def new_id(prefix: str) -> str:
    return prefix + secrets.token_hex(4)


def fts_query(text: str, max_terms: int = 24) -> str:
    """Свободный текст -> запрос FTS5: значимые слова через OR, каждое в кавычках."""
    seen, terms = set(), []
    for w in _WORD.findall(text.lower()):
        if len(w) < 2 or w in _STOP or w in seen:
            continue
        seen.add(w)
        terms.append('"' + w.replace('"', "") + '"')
        if len(terms) >= max_terms:
            break
    return " OR ".join(terms)


def answer_score(worked: int, failed: int) -> float:
    """Доля «сработало» со сглаживанием: новый ответ 0.5, проверенный растёт."""
    return (worked + 1) / (worked + failed + 2)


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("pragma journal_mode=wal")
        self.conn.execute("pragma foreign_keys=on")
        self.conn.executescript(SCHEMA)
        self.lock = threading.RLock()

    # --- запись ---------------------------------------------------------

    def add_question(self, title: str, body: str, context: dict, tags: list[str]) -> str:
        qid, now = new_id("q"), time.time()
        with self.lock:
            self.conn.execute(
                "insert into questions(id,title,body,context,tags,created_at,updated_at) values(?,?,?,?,?,?,?)",
                (qid, title, body, json.dumps(context, ensure_ascii=False), ",".join(tags), now, now))
            self.reindex(qid)
        return qid

    def update_question(self, qid: str, *, title: str | None = None, tags: list[str] | None = None):
        with self.lock:
            if title:
                self.conn.execute("update questions set title=? where id=?", (title, qid))
            if tags is not None:
                old = self.question_row(qid)
                merged = sorted(set(filter(None, (old["tags"] or "").split(","))) | set(tags))
                self.conn.execute("update questions set tags=? where id=?", (",".join(merged), qid))
            self.conn.execute("update questions set updated_at=? where id=?", (time.time(), qid))
            self.reindex(qid)

    def add_answer(self, qid: str, body: str, context: dict, author: str = "") -> str:
        aid = new_id("a")
        with self.lock:
            self.conn.execute(
                "insert into answers(id,question_id,body,context,created_at,author) values(?,?,?,?,?,?)",
                (aid, qid, body, json.dumps(context, ensure_ascii=False), time.time(), author))
            self.conn.execute("update questions set updated_at=? where id=?", (time.time(), qid))
            self.reindex(qid)
        return aid

    def add_aliases(self, qid: str, texts: list[str], kind: str) -> int:
        n = 0
        with self.lock:
            for t in texts:
                t = (t or "").strip()
                if not t:
                    continue
                cur = self.conn.execute(
                    "insert or ignore into aliases(question_id,text,kind,created_at) values(?,?,?,?)",
                    (qid, t[:500], kind, time.time()))
                n += cur.rowcount
            if n:
                self.reindex(qid)
        return n

    def reindex(self, qid: str):
        with self.lock:
            q = self.question_row(qid)
            if not q:
                return
            aliases = " \n".join(r["text"] for r in self.conn.execute(
                "select text from aliases where question_id=?", (qid,)))
            answers = " \n".join(r["body"] for r in self.conn.execute(
                "select body from answers where question_id=?", (qid,)))
            self.conn.execute("delete from qfts where qid=?", (qid,))
            self.conn.execute(
                "insert into qfts(qid,title,body,aliases,answers,tags) values(?,?,?,?,?,?)",
                (qid, q["title"], q["body"], aliases, answers, q["tags"].replace(",", " ")))

    def vote(self, aid: str, worked: bool, note: str = "", search_id: str | None = None) -> bool:
        col = "worked" if worked else "failed"
        with self.lock:
            cur = self.conn.execute(f"update answers set {col}={col}+1 where id=?", (aid,))
            if not cur.rowcount:
                return False
            self.conn.execute(
                "insert into votes(answer_id,worked,note,created_at,search_id) values(?,?,?,?,?)",
                (aid, int(worked), note, time.time(), search_id))
        return True

    def log_search(self, query: str, context: dict, result_ids: list[str]) -> str:
        sid = new_id("s")
        with self.lock:
            self.conn.execute(
                "insert into searches(id,query,context,result_ids,created_at) values(?,?,?,?,?)",
                (sid, query, json.dumps(context, ensure_ascii=False), json.dumps(result_ids), time.time()))
            for qid in result_ids:
                self.conn.execute("update questions set hits=hits+1 where id=?", (qid,))
        return sid

    def resolve_search(self, sid: str, qid: str):
        with self.lock:
            self.conn.execute("update searches set resolved_question_id=? where id=?", (qid, sid))

    def mark_gardened(self, sid: str):
        with self.lock:
            self.conn.execute("update searches set gardened=1 where id=?", (sid,))

    # --- чтение ---------------------------------------------------------

    def question_row(self, qid: str):
        return self.conn.execute("select * from questions where id=?", (qid,)).fetchone()

    def search_row(self, sid: str):
        return self.conn.execute("select * from searches where id=?", (sid,)).fetchone()

    def fts(self, text: str, limit: int = 20) -> list[tuple[str, float]]:
        q = fts_query(text)
        if not q:
            return []
        with self.lock:
            rows = self.conn.execute(
                f"select qid, {_BM25} as rank from qfts where qfts match ? order by rank limit ?",
                (q, limit)).fetchall()
        # bm25 в sqlite отрицательный: чем меньше, тем лучше. Переворачиваем.
        return [(r["qid"], -r["rank"]) for r in rows]

    def question(self, qid: str, answers_limit: int = 5) -> dict | None:
        q = self.question_row(qid)
        if not q:
            return None
        ans = [dict(r) for r in self.conn.execute(
            "select id,body,context,created_at,worked,failed from answers where question_id=?", (qid,))]
        for a in ans:
            a["context"] = json.loads(a["context"] or "{}")
            a["score"] = round(answer_score(a["worked"], a["failed"]), 3)
        ans.sort(key=lambda a: (a["score"], a["worked"], a["created_at"]), reverse=True)
        return {
            "id": q["id"], "title": q["title"], "body": q["body"],
            "context": json.loads(q["context"] or "{}"),
            "tags": [t for t in q["tags"].split(",") if t],
            "created_at": q["created_at"], "updated_at": q["updated_at"], "hits": q["hits"],
            "answers": ans[:answers_limit] if answers_limit else ans,
        }

    def aliases(self, qid: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "select text,kind from aliases where question_id=? order by created_at", (qid,))]

    def misses(self, older_than: float = 300, limit: int = 50) -> list[sqlite3.Row]:
        """Поиски, после которых агент ничего не нашёл и ничего не оставил."""
        return self.conn.execute(
            "select * from searches where gardened=0 and resolved_question_id is null "
            "and result_ids='[]' and created_at < ? order by created_at limit ?",
            (time.time() - older_than, limit)).fetchall()

    def recent(self, limit: int = 30) -> list[dict]:
        rows = self.conn.execute(
            "select q.id, q.title, q.tags, q.updated_at, q.hits, "
            "(select count(*) from answers a where a.question_id=q.id) as n_answers, "
            "(select coalesce(sum(worked),0) from answers a where a.question_id=q.id) as worked "
            "from questions q order by q.updated_at desc limit ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        one = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "questions": one("select count(*) from questions"),
            "answers": one("select count(*) from answers"),
            "aliases": one("select count(*) from aliases"),
            "searches": one("select count(*) from searches"),
            "hit_rate": round(one(
                "select avg(case when result_ids!='[]' then 1.0 else 0 end) from searches") or 0, 3),
            "votes_worked": one("select count(*) from votes where worked=1"),
            "votes_failed": one("select count(*) from votes where worked=0"),
        }
