"""Explicit, one-time expansion of the deployed A3 8-pair reservation model.

Run with the queue proxy stopped and after a consistent SQLite backup. This
changes reservations only; no workload or account operations are performed.
"""
import json
import sqlite3


def expand_cards(cards):
    if (not isinstance(cards, list) or len(set(cards)) != len(cards)
            or any(type(card) is not int or not 0 <= card < 8 for card in cards)):
        raise ValueError('Invalid original A3 paired-card allocation')
    return [logical for physical in cards for logical in (2*physical, 2*physical+1)]


def migrate(database, nodes, *, source_layout=None):
    if source_layout != 'a3-paired8':
        raise ValueError('Explicit A3 paired-8 source layout is required; never migrate A5 this way')
    if len(set(nodes)) != 2:
        raise ValueError('Exactly two distinct nodes are required')
    from pathlib import Path
    path = Path(database).resolve(strict=True)
    db = sqlite3.connect(path.as_uri()+'?mode=rw', uri=True, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN IMMEDIATE')
        capacity = db.execute("SELECT value FROM unified_meta WHERE key='cards_per_node'").fetchone()
        marker = db.execute("SELECT value FROM unified_meta WHERE key='a3_logical16_migration'").fetchone()
        if capacity and capacity[0] == '16' and marker and marker[0] == '1':
            db.rollback()
            return {'changed': False, 'tasks': 0, 'shared_grants': 0}
        if marker or (capacity and capacity[0] != '8'):
            raise ValueError('Unexpected card geometry; refusing to guess a migration')
        tasks = db.execute('SELECT id,cards,allocations FROM unified_tasks').fetchall()
        for task in tasks:
            if type(task['cards']) is not int or not 1 <= task['cards'] <= 8:
                raise ValueError('Invalid old reservation count')
            allocation = json.loads(task['allocations'])
            if not isinstance(allocation, dict) or not set(allocation) <= set(nodes):
                raise ValueError('Unknown allocation node')
            expanded = {node: expand_cards(cards) for node, cards in allocation.items()}
            db.execute('UPDATE unified_tasks SET cards=?,allocations=? WHERE id=?',
                       (2*task['cards'], json.dumps(expanded), task['id']))
        grants = db.execute('SELECT * FROM unified_shared_continuing').fetchall()
        for grant in grants:
            if grant['node'] not in nodes:
                raise ValueError('Unknown continuing-grant node')
            db.execute('UPDATE unified_shared_continuing SET cards=? WHERE node=? AND employee=?',
                       (json.dumps(expand_cards(json.loads(grant['cards']))), grant['node'], grant['employee']))
        db.execute("INSERT OR REPLACE INTO unified_meta VALUES ('cards_per_node','16')")
        db.execute("INSERT INTO unified_meta VALUES ('a3_logical16_migration','1')")
        db.commit()
        return {'changed': True, 'tasks': len(tasks), 'shared_grants': len(grants)}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--nodes', nargs=2, required=True)
    parser.add_argument('--from-layout', choices=['a3-paired8'], required=True,
                        help='Only for A3 deployments where old slot x meant logical IDs 2x and 2x+1')
    args = parser.parse_args()
    print(json.dumps(migrate(args.database, args.nodes, source_layout=args.from_layout)))
