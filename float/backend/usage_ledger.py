"""Local numeric usage ledger. Never stores prompts, messages or reasoning text."""
import json
from pathlib import Path
import sqlite3


class UsageLedger:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(path), timeout=.3)
        path.chmod(0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA cache_size=-2048")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS requests (
                thread TEXT NOT NULL, response TEXT NOT NULL, turn TEXT,
                model TEXT, price_hash TEXT, observed_at REAL,
                input INTEGER, cached INTEGER, output INTEGER, reasoning INTEGER,
                usd REAL, credits REAL, baseline_usd REAL, baseline_credits REAL,
                PRIMARY KEY(thread,response));
            CREATE INDEX IF NOT EXISTS requests_turn ON requests(thread,turn);
            CREATE TABLE IF NOT EXISTS checkpoints (
                thread TEXT PRIMARY KEY, path TEXT, inode INTEGER, offset INTEGER,
                model TEXT, skipping INTEGER, skipped INTEGER, lifecycle TEXT);
        """)

    def contains(self, thread, response):
        return self.db.execute("SELECT 1 FROM requests WHERE thread=? AND response=?",
                               (thread, response)).fetchone() is not None

    def add(self, thread, response, turn, model, usage, actual, baseline, price_hash, observed_at):
        result = self.db.execute("INSERT OR IGNORE INTO requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            thread, response, turn, model, price_hash, observed_at,
            usage['input_tokens'], usage['cached_input_tokens'], usage['output_tokens'],
            usage['reasoning_output_tokens'], actual[0] if actual else None,
            actual[1] if actual else None, baseline[0] if actual and baseline else None,
            baseline[1] if actual and actual[1] is not None and baseline else None))
        return result.rowcount == 1

    def totals(self, thread, turn=None):
        query = """SELECT COUNT(*),COALESCE(SUM(input),0),COALESCE(SUM(cached),0),
            COALESCE(SUM(output),0),COALESCE(SUM(reasoning),0),COUNT(usd),
            COALESCE(SUM(CASE WHEN usd IS NOT NULL THEN input+output ELSE 0 END),0),
            COALESCE(SUM(usd),0),COALESCE(SUM(credits),0),
            COALESCE(SUM(baseline_usd),0),COALESCE(SUM(baseline_credits),0),
            COUNT(DISTINCT price_hash),COUNT(credits),
            COALESCE(SUM(CASE WHEN credits IS NOT NULL THEN input+output ELSE 0 END),0)
            FROM requests WHERE thread=?"""
        params = [thread]
        if turn is not None:
            query += " AND turn=?"
            params.append(turn)
        r = self.db.execute(query, params).fetchone()
        return dict(requests=r[0], inputTokens=r[1], cachedInputTokens=r[2],
                    outputTokens=r[3], reasoningTokens=r[4], totalTokens=r[1]+r[3],
                    pricedRequests=r[5], pricedTokens=r[6], usd=r[7], credits=r[8],
                    baseline=r[9], baselineCredits=r[10], priceVersions=r[11],
                    creditRequests=r[12], referencePricedTokens=r[13])

    def checkpoint(self, thread, reader):
        if not reader.identity:
            return
        # Re-read the unfinished suffix on resume; never persist its bytes.
        self.db.execute("INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?,?,?,?,?)", (
            thread, reader.identity[0], reader.identity[1], reader.offset-len(reader.partial),
            reader.model, int(reader.skipping), reader.skipped,
            json.dumps(reader.turn, separators=(',', ':'))))
        self.db.commit()

    def restore(self, thread):
        row = self.db.execute("SELECT path,inode,offset,model,skipping,skipped,lifecycle FROM checkpoints WHERE thread=?",
                              (thread,)).fetchone()
        if row:
            return dict(identity=(row[0], row[1]), offset=row[2], model=row[3],
                        skipping=bool(row[4]), skipped=row[5], turn=json.loads(row[6]))
        return {}

    def close(self):
        self.db.commit()
        self.db.close()
