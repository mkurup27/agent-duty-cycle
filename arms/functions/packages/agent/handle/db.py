"""Tiny DB wrapper: Postgres (pg8000, pure Python) in production, SQLite for
local tests. SQL is written with %s placeholders.

DATABASE_URL  postgresql://user:pass@host:25060/db   (DO Managed Postgres)
              sqlite:////tmp/agent.db                 (local tests)
PG_CA_CERT    contents of the cluster's ca-certificate.crt (recommended)
"""

import os
import ssl
import urllib.parse

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_state (
  agent_id       TEXT PRIMARY KEY,
  version        INTEGER NOT NULL,
  schema_version INTEGER NOT NULL,
  state_json     TEXT NOT NULL,
  ws_key         TEXT,
  ws_sig         TEXT,
  ws_bytes       BIGINT,
  updated_at     DOUBLE PRECISION
)"""


class Db:
    def __init__(self, url=None):
        url = url or os.environ["DATABASE_URL"]
        u = urllib.parse.urlsplit(url)
        if u.scheme == "sqlite":
            import sqlite3
            self.kind = "sqlite"
            self.IntegrityError = sqlite3.IntegrityError
            self.conn = sqlite3.connect(u.path, isolation_level=None, timeout=30)
        else:
            import pg8000.dbapi as pg
            self.kind = "pg"
            self.IntegrityError = pg.IntegrityError
            ca = (os.environ.get("PG_CA_CERT") or "").replace("\\n", "\n") or None
            ctx = ssl.create_default_context(cadata=ca) if ca else ssl.create_default_context()
            if not ca and os.environ.get("PG_SSL_INSECURE") == "1":
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            self.conn = pg.connect(
                user=urllib.parse.unquote(u.username or ""),
                password=urllib.parse.unquote(u.password or ""),
                host=u.hostname, port=u.port or 5432,
                database=u.path.lstrip("/") or "defaultdb", ssl_context=ctx)
            self.conn.autocommit = True

    def _sql(self, sql):
        return sql.replace("%s", "?") if self.kind == "sqlite" else sql

    def ensure_schema(self):
        self.execute(SCHEMA)

    def execute(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(self._sql(sql), params)
        n = cur.rowcount
        cur.close()
        return n

    def fetchone(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(self._sql(sql), params)
        row = cur.fetchone()
        cur.close()
        return row

    def ping(self):
        try:
            self.fetchone("SELECT 1")
            return True
        except Exception:  # noqa: BLE001
            return False
