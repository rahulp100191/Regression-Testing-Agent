"""Durable PostgreSQL evaluation queue and immutable run snapshots."""
import os
from contextlib import contextmanager, nullcontext
from telemetry import stage, event

_initialized = False

class TracedConnection:
    def __init__(self, connection):
        self.connection = connection
    def execute(self, query, params=None):
        # Only the SQL verb is traced. Values and SQL text can contain private data.
        operation = query.strip().split()[0].upper()
        with stage('postgres.query', db_system='postgresql', db_operation=operation):
            result = self.connection.execute(query,params)
            event('postgres.query_completed',operation=operation,row_count=result.rowcount)
            return result
    def __getattr__(self, name):
        return getattr(self.connection,name)

@contextmanager
def database(traced=True):
    with stage('postgres.session') if traced else nullcontext():
        with _database(traced) as connection:
            yield connection

@contextmanager
def _database(traced):
    global _initialized
    import psycopg
    from psycopg.rows import dict_row
    options = {"host": os.environ["PGHOST"], "port": os.getenv("PGPORT", "5432"),
               "dbname": os.environ["PGDATABASE"], "user": os.environ["PGUSER"],
               "password": os.environ["PGPASSWORD"]} if os.getenv("PGHOST") else {}
    with psycopg.connect("" if options else os.environ["DATABASE_URL"], connect_timeout=10,
                         row_factory=dict_row, **options) as connection:
        if not _initialized:
            # Serialize API/worker bootstrap to avoid concurrent system-catalog races.
            connection.execute('SELECT pg_advisory_xact_lock(814006)')
            connection.execute("""CREATE TABLE IF NOT EXISTS eval_runs (
              eval_run_id uuid PRIMARY KEY, status text NOT NULL,
              timestamp timestamptz NOT NULL DEFAULT now(), completed_at timestamptz,
              heartbeat timestamptz, snapshot jsonb NOT NULL, report jsonb, error text)""")
            connection.execute("ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS progress jsonb")
            connection.execute("""CREATE TABLE IF NOT EXISTS analysis_rows (
              row_id uuid PRIMARY KEY, run_id uuid NOT NULL, source_row integer NOT NULL,
              created_at timestamptz NOT NULL DEFAULT now(), release jsonb NOT NULL,
              catalog jsonb NOT NULL, prediction jsonb NOT NULL, versions jsonb NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS row_feedback (
              feedback_id uuid PRIMARY KEY, row_id uuid NOT NULL REFERENCES analysis_rows(row_id),
              created_at timestamptz NOT NULL DEFAULT now(), author text NOT NULL,
              correct boolean NOT NULL, expected_decision text NOT NULL,
              expected_tests jsonb NOT NULL, missed_tests jsonb NOT NULL, comment text NOT NULL,
              status text NOT NULL DEFAULT 'pending', reviewer text, review_comment text,
              reviewed_at timestamptz)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS golden_cases (
              case_id uuid PRIMARY KEY, row_id uuid NOT NULL UNIQUE REFERENCES analysis_rows(row_id),
              feedback_id uuid NOT NULL UNIQUE REFERENCES row_feedback(feedback_id),
              created_at timestamptz NOT NULL DEFAULT now(), case_data jsonb NOT NULL)""")
            connection.execute('CREATE INDEX IF NOT EXISTS feedback_status_created ON row_feedback(status,created_at DESC)')
            connection.execute('CREATE INDEX IF NOT EXISTS analysis_run ON analysis_rows(run_id)')
            connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS analysis_run_source_unique ON analysis_rows(run_id,source_row)')
            connection.commit()
            _initialized = True
        yield TracedConnection(connection) if traced else connection

def encode(value):
    from psycopg.types.json import Jsonb
    return Jsonb(value)
