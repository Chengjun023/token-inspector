import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / 'backend'))
from meter import UsageReader, MAX_IDS
from usage_ledger import UsageLedger


class PersistentMeterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / 'rollout.jsonl'
        self.ledger = UsageLedger(self.root / 'counts.sqlite3')

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def events(self, count=1):
        return [
            {'type':'turn_context','payload':{'model':'gpt-6.1-sol','prompt':'PRIVATE_BODY'}},
            {'type':'event_msg','timestamp':'2026-09-30T04:00:00Z',
             'payload':{'type':'task_started','turn_id':'turn-1','text':'PRIVATE_BODY'}},
        ] + [{'type':'token_usage_record','payload':{'thread_id':'t','turn_id':'turn-1',
            'response_id':str(i),'usage':{'input_tokens':1000,'cached_input_tokens':800,'output_tokens':100}}}
             for i in range(count)]

    def write(self, events):
        self.path.write_text(''.join(json.dumps(e)+'\n' for e in events))

    def read_all(self, reader):
        while reader.read(self.path):
            pass

    def test_restart_and_rotation_preserve_counts_without_duplicates(self):
        self.write(self.events())
        first = UsageReader('t', self.ledger)
        self.read_all(first)
        saved = first.summary()
        self.ledger.close()
        self.ledger = UsageLedger(self.root / 'counts.sqlite3')
        second = UsageReader('t', self.ledger)
        self.read_all(second)
        self.assertEqual(saved, second.summary())
        rotated = self.root / 'rotated.jsonl'
        rotated.write_text(self.path.read_text())
        second.read(rotated)
        self.assertEqual(second.summary()['requests'], 1)
        self.assertEqual(second.summary()['currentTurn']['totalTokens'], 1100)

    def test_long_thread_has_no_in_memory_id_cap(self):
        self.write(self.events(MAX_IDS+3))
        reader = UsageReader('t', self.ledger)
        self.read_all(reader)
        self.assertEqual(reader.summary()['requests'], MAX_IDS+3)
        self.assertFalse(reader.capped)
        self.assertEqual(len(reader.seen), 0)

    def test_checkpoint_does_not_persist_partial_content(self):
        self.write(self.events())
        suffix = json.dumps({'type':'response_item','payload':{'body':'PRIVATE_BODY'}})
        with self.path.open('a') as f:
            f.write(suffix[:45])
        first = UsageReader('t', self.ledger)
        self.read_all(first)
        second = UsageReader('t', self.ledger)
        with self.path.open('a') as f:
            f.write(suffix[45:]+'\n')
        self.read_all(second)
        self.assertEqual(second.summary()['requests'], 1)
        for table in ('requests','checkpoints'):
            rows = self.ledger.db.execute('SELECT * FROM '+table).fetchall()
            self.assertNotIn('PRIVATE_BODY', repr(rows))

    def test_repricing_does_not_change_already_recorded_cost(self):
        self.write(self.events())
        reader = UsageReader('t', self.ledger)
        self.read_all(reader)
        cost = reader.summary()['costUSD']
        class ChangedPrices:
            version = 'new-price'
            def price(self, usage, model): return (99, 99)
        restarted = UsageReader('t', self.ledger, ChangedPrices())
        self.read_all(restarted)
        self.assertEqual(restarted.summary()['costUSD'], cost)

    def test_failed_checkpoint_rewinds_and_retries_without_losing_usage(self):
        self.write(self.events())
        reader = UsageReader('t', self.ledger)
        checkpoint = self.ledger.checkpoint
        def fail(*args):
            raise sqlite3.OperationalError('database is locked')
        self.ledger.checkpoint = fail
        with self.assertRaises(sqlite3.OperationalError):
            reader.read(self.path)
        self.assertEqual(reader.offset, 0)
        self.assertEqual(reader.summary()['requests'], 0)
        self.ledger.checkpoint = checkpoint
        self.read_all(reader)
        self.assertEqual(reader.summary()['requests'], 1)
        self.assertFalse(reader.persistence_error)

    def test_two_readers_refresh_totals_after_other_instance_writes(self):
        self.write(self.events())
        first = UsageReader('t', self.ledger)
        second = UsageReader('t', self.ledger)
        self.read_all(first)
        self.read_all(second)
        self.assertEqual(second.summary()['requests'], 1)


if __name__ == '__main__':
    unittest.main()
