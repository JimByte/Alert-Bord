# database.py (v9.1 - Simplified, No User Management)
import sqlite3
import flask
from flask import g
import logging
import json

logger = logging.getLogger(__name__)
DATABASE = 'alerts.db'

def get_db():
    if 'db' not in g:
        try:
            g.db = sqlite3.connect(DATABASE, detect_types=sqlite3.PARSE_DECLTYPES)
            g.db.row_factory = sqlite3.Row
            logger.debug("Database connection opened.")
        except sqlite3.Error as e:
            logger.error(f"Error connecting to database {DATABASE}: {e}", exc_info=True)
            raise
    return g.db

def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()
        logger.debug("Database connection closed.")

def register_db_hooks(app):
    app.teardown_appcontext(close_db)

def init_db(app):
    db = None
    try:
        db = sqlite3.connect(DATABASE, detect_types=sqlite3.PARSE_DECLTYPES)
        cursor = db.cursor()
        logger.info(f"Initializing database schema in '{DATABASE}'...")
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS alerts (
                id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, severity TEXT, alert_group TEXT, alertname TEXT, message TEXT,
                source_ip TEXT, alertmanager_status TEXT DEFAULT 'firing', resolved_at TEXT, alertmanager_labels TEXT,
                alertmanager_annotations TEXT, alertmanager_groupLabels TEXT, acknowledged INTEGER DEFAULT 0,
                acknowledged_by TEXT, acknowledged_at TEXT, acknowledge_comment TEXT
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON alerts (timestamp DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts (alertmanager_status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_alerts_alertname ON alerts (alertname)')
        logger.info("Table 'alerts' initialized.")
        db.commit()
        logger.info("Database schema initialization complete (alerts table only).")
    except sqlite3.Error as e:
        logger.error(f"An error occurred during database initialization: {e}", exc_info=True)
        if db: db.rollback()
    finally:
        if db: db.close()

def row_to_dict(row):
    if not row: return None
    d = dict(row)
    if 'alertmanager_labels' in d:
        for key in ['alertmanager_labels', 'alertmanager_annotations', 'alertmanager_groupLabels']:
            if d.get(key) and isinstance(d[key], str):
                try: d[key] = json.loads(d[key])
                except json.JSONDecodeError: d[key] = {}
            elif not d.get(key): d[key] = {}
        d['acknowledged'] = bool(d.get('acknowledged', 0))
    return d

def read_alerts_from_db(limit=None):
    alerts = []
    try:
        db = get_db()
        query = "SELECT * FROM alerts ORDER BY timestamp DESC"
        if limit and isinstance(limit, int) and limit > 0: query += f" LIMIT {limit}"
        cursor = db.execute(query)
        alerts = [row_to_dict(row) for row in cursor.fetchall()]
    except Exception as e: logger.error(f"Error reading alerts: {e}", exc_info=True)
    return alerts

def find_alert_by_id(alert_id):
    try: db = get_db(); cursor = db.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)); return row_to_dict(cursor.fetchone())
    except Exception as e: logger.error(f"Error finding alert ID {alert_id}: {e}", exc_info=True); return None

def insert_alert_to_db(alert_data):
    labels_json = json.dumps(alert_data.get('alertmanager_labels', {}))
    annotations_json = json.dumps(alert_data.get('alertmanager_annotations', {}))
    groupLabels_json = json.dumps(alert_data.get('alertmanager_groupLabels', {}))
    alert_data.setdefault('acknowledged', 0); alert_data.setdefault('acknowledged_by', None); alert_data.setdefault('acknowledged_at', None); alert_data.setdefault('acknowledge_comment', None)
    sql = ''' INSERT INTO alerts(id, timestamp, severity, alert_group, alertname, message, source_ip, alertmanager_status, resolved_at, alertmanager_labels, alertmanager_annotations, alertmanager_groupLabels, acknowledged, acknowledged_by, acknowledged_at, acknowledge_comment) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '''
    params = (alert_data.get('id'), alert_data.get('timestamp'), alert_data.get('severity'), alert_data.get('alert_group'), alert_data.get('alertname'), alert_data.get('message'), alert_data.get('source_ip'), alert_data.get('alertmanager_status', 'firing'), alert_data.get('resolved_at'), labels_json, annotations_json, groupLabels_json, int(alert_data['acknowledged']), alert_data['acknowledged_by'], alert_data['acknowledged_at'], alert_data['acknowledge_comment'])
    try: db = get_db(); db.execute(sql, params); db.commit(); return True
    except sqlite3.IntegrityError: logger.warning(f"Alert ID {alert_data.get('id')} already exists."); return False
    except Exception as e: logger.error(f"DB Error inserting alert ID {alert_data.get('id')}: {e}", exc_info=True); db.rollback(); return False

def update_alert_in_db(alert_id, updates):
    if not updates: return False
    update_copy = updates.copy()
    if 'alertmanager_labels' in update_copy: update_copy['alertmanager_labels'] = json.dumps(update_copy['alertmanager_labels'])
    if 'alertmanager_annotations' in update_copy: update_copy['alertmanager_annotations'] = json.dumps(update_copy['alertmanager_annotations'])
    if 'alertmanager_groupLabels' in update_copy: update_copy['alertmanager_groupLabels'] = json.dumps(update_copy['alertmanager_groupLabels'])
    if 'acknowledged' in update_copy: update_copy['acknowledged'] = int(update_copy['acknowledged'])
    if 'acknowledged_by' in update_copy and update_copy['acknowledged_by'] is None: pass
    elif 'acknowledged_by' in update_copy: del update_copy['acknowledged_by']
    set_clause = ", ".join([f"{key} = ?" for key in update_copy.keys()])
    params = list(update_copy.values()) + [alert_id]
    sql = f"UPDATE alerts SET {set_clause} WHERE id = ?"
    success = False
    try:
        db = get_db(); cursor = db.execute(sql, params)
        if cursor.rowcount > 0: db.commit(); success = True; logger.debug(f"Updated alert ID {alert_id} with fields: {list(update_copy.keys())}")
        else: logger.warning(f"Update failed: alert ID {alert_id} not found.")
    except Exception as e: logger.error(f"DB Error updating alert ID {alert_id}: {e}", exc_info=True); db.rollback()
    return success

def prune_alerts_db():
    retention_limit = 10000
    try:
        with open('settings.json', 'r', encoding='utf-8') as f: settings = json.load(f); loaded_value = settings.get('max_alerts_retention');
        if isinstance(loaded_value, int) and loaded_value >= 100: retention_limit = loaded_value
    except Exception: pass
    logger.debug(f"Checking prune requirement (limit: {retention_limit})...")
    try:
        db = get_db(); cursor = db.execute("SELECT timestamp FROM alerts ORDER BY timestamp DESC LIMIT 1 OFFSET ?", (retention_limit -1,)); cutoff_row = cursor.fetchone()
        if cutoff_row:
            cutoff_timestamp = cutoff_row['timestamp']; logger.info(f"Pruning alerts older than or equal to: {cutoff_timestamp}")
            delete_cursor = db.execute("DELETE FROM alerts WHERE timestamp <= ?", (cutoff_timestamp,)); deleted_count = delete_cursor.rowcount; db.commit(); logger.info(f"Pruning complete. Deleted {deleted_count} old alerts.")
        else: logger.debug("Not enough alerts to require pruning.")
    except Exception as e: logger.error(f"DB Error pruning alerts: {e}", exc_info=True); db.rollback()

def get_unique_groups_db():
    groups = set()
    try:
        db = get_db(); cursor = db.execute("SELECT DISTINCT alert_group FROM alerts WHERE alert_group IS NOT NULL AND alert_group != ''"); groups = set(row['alert_group'] for row in cursor.fetchall())
        if 'Default' not in groups:
            cursor_default = db.execute("SELECT 1 FROM alerts WHERE alert_group IS NULL OR alert_group = '' OR alert_group = 'Default' LIMIT 1")
            if cursor_default.fetchone(): groups.add('Default')
    except Exception as e: logger.error(f"DB Error getting unique groups: {e}", exc_info=True)
    return sorted(list(groups))

def alerts_match(new_alert_labels, existing_alert_labels):
    if not new_alert_labels or not existing_alert_labels: return False
    if new_alert_labels.get('alertname') != existing_alert_labels.get('alertname'): return False
    identifying_keys_new = set(new_alert_labels.keys()) - {'severity'}
    identifying_keys_existing = set(existing_alert_labels.keys()) - {'severity'}
    if identifying_keys_new != identifying_keys_existing: return False
    for key in identifying_keys_new:
        if new_alert_labels.get(key) != existing_alert_labels.get(key): return False
    return True

def find_matching_firing_alert_db(alert_labels):
     alertname = alert_labels.get('alertname');
     if not alertname: logger.debug("Cannot find match: alertname missing."); return None
     try:
        db = get_db(); cursor = db.execute("SELECT * FROM alerts WHERE alertname = ? AND alertmanager_status = 'firing' ORDER BY timestamp DESC LIMIT 50", (alertname,))
        candidates = [row_to_dict(row) for row in cursor.fetchall()]
        for existing_alert in candidates:
            if alerts_match(alert_labels, existing_alert.get('alertmanager_labels', {})): return existing_alert
        return None
     except Exception as e: logger.error(f"DB Error finding match for {alertname}: {e}", exc_info=True); return None