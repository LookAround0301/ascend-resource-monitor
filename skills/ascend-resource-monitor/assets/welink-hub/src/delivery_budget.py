"""Persistent sender quota and cooldown, without message or credential data."""
from contextlib import closing
import sqlite3
import time


class DeliveryDeferred(Exception):
    """No request was sent, or HTTP 429 definitely rejected it."""


class DeliveryBudget:
    def __init__(self,path,limit=450):
        self.path=path;self.limit=limit
        with closing(sqlite3.connect(path)) as db,db:
            db.execute('CREATE TABLE IF NOT EXISTS send_attempts (at REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS send_cooldown (id INTEGER PRIMARY KEY,until REAL NOT NULL)')

    def delay(self,now=None):
        now=time.time() if now is None else now
        with closing(sqlite3.connect(self.path)) as db:
            row=db.execute('SELECT until FROM send_cooldown WHERE id=1').fetchone()
        return max(0,row[0]-now) if row else 0

    def reserve(self,now=None):
        now=time.time() if now is None else now
        with closing(sqlite3.connect(self.path,timeout=5)) as db,db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT until FROM send_cooldown WHERE id=1').fetchone()
            if row and row[0]>now:return False
            db.execute('DELETE FROM send_attempts WHERE at<=?',(now-86400,))
            count,first=db.execute('SELECT count(*),min(at) FROM send_attempts').fetchone()
            if self.limit and count>=self.limit:
                db.execute('INSERT OR REPLACE INTO send_cooldown VALUES (1,?)',(first+86400,))
                return False
            db.execute('INSERT INTO send_attempts VALUES (?)',(now,))
            return True

    def throttled(self,seconds=900,now=None):
        now=time.time() if now is None else now
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('INSERT INTO send_cooldown VALUES (1,?) ON CONFLICT(id) DO UPDATE SET until=max(until,excluded.until)',(now+seconds,))
