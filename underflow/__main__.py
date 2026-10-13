"""underflow serve | garden | seed FILE | stats"""

import argparse
import json
import logging
import os
import sys

from .config import Config
from .core import Rejected, Underflow


def main(argv=None):
    ap = argparse.ArgumentParser(prog="underflow", description="Stack Overflow for agents: server side")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the HTTP API")
    s.add_argument("--host", default=os.environ.get("UNDERFLOW_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("UNDERFLOW_PORT", "8800")))
    sub.add_parser("garden", help="one gardener pass: link search misses, curate raw entries")
    sd = sub.add_parser("seed", help="load question/answer pairs from a JSONL file")
    sd.add_argument("file")
    sub.add_parser("stats", help="print base stats")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config()

    if a.cmd == "serve":
        import uvicorn
        from .server import create_app
        uvicorn.run(create_app(cfg), host=a.host, port=a.port, log_level="info")
        return

    uf = Underflow(cfg)
    if a.cmd == "garden":
        print(json.dumps(uf.garden(), ensure_ascii=False))
    elif a.cmd == "stats":
        print(json.dumps({**uf.db.stats(), "librarian": uf.lib.mode}, ensure_ascii=False, indent=2))
    elif a.cmd == "seed":
        with open(a.file, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if not line.strip():
                    continue
                e = json.loads(line)
                try:
                    r = uf.post(e.get("title", ""), e.get("problem", ""), e["answer"],
                                e.get("context", {}), e.get("tags", []), author=e.get("author", "seed"))
                    print(f"{n}: {r['status']} {r['question_id']} {r['title']}")
                except Rejected as err:
                    print(f"{n}: rejected: {err}", file=sys.stderr)


if __name__ == "__main__":
    main()
