# main.py (v9.1 - Simplified, Renamed, "Alert Bord")

import flask
from flask import Flask, request, jsonify, render_template, redirect, url_for, Response, flash, g
import json
import datetime
from datetime import datetime as dt, timedelta
import uuid
import os
import csv
import io
import logging
import jdatetime
import sqlite3
import threading
import time

# --- Database Setup ---
import database
from database import (
    DATABASE, get_db, init_db, register_db_hooks, row_to_dict, alerts_match,
    read_alerts_from_db, find_alert_by_id, insert_alert_to_db, update_alert_in_db,
    prune_alerts_db, get_unique_groups_db, find_matching_firing_alert_db
)

# --- Prometheus Metrics ---
from prometheus_flask_exporter import PrometheusMetrics
from prometheus_client import Gauge, Counter

# --- Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s:%(name)s:%(message)s')
logger = logging.getLogger(__name__)

SETTINGS_FILE = 'settings.json'
DEFAULT_RETENTION = 5000

# --- Flask App Initialization ---
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', os.urandom(16))
if len(app.secret_key) < 16:
    logger.warning("FLASK_SECRET_KEY is short or temporary.")

# --- Register DB Hooks ---
database.register_db_hooks(app)

# --- Initialize Prometheus Metrics ---
metrics = PrometheusMetrics(app, group_by='endpoint', path='/metrics')
metrics.info('alert_bord_info', 'Alert Bord Application info', version='9.1-simplified')

# --- Custom Prometheus Gauges & Counters ---
metrics_lock = threading.Lock()
alerts_total_gauge = Gauge('alert_dashboard_alerts_total', 'Total number of alerts in the database')
alerts_firing_unacked_gauge = Gauge('alert_dashboard_alerts_firing_unacknowledged_total', 'Number of firing and unacknowledged alerts')
alerts_firing_acked_gauge = Gauge('alert_dashboard_alerts_firing_acknowledged_total', 'Number of firing and acknowledged alerts')
alerts_resolved_gauge = Gauge('alert_dashboard_alerts_resolved_total', 'Number of resolved alerts')
webhook_received_counter = Counter('alert_dashboard_webhook_received_total', 'Total webhooks received')
webhook_errors_counter = Counter('alert_dashboard_webhook_errors_total', 'Total errors processing webhooks', ['reason'])
acknowledge_requests_counter = Counter('alert_dashboard_acknowledge_requests_total', 'Total acknowledge requests')
acknowledge_errors_counter = Counter('alert_dashboard_acknowledge_errors_total', 'Total errors during acknowledge requests', ['reason'])

# --- Settings Management ---
def load_settings():
    default_settings = {"max_alerts_retention": DEFAULT_RETENTION}
    if not os.path.exists(SETTINGS_FILE): logger.info(f"{SETTINGS_FILE} not found, creating with defaults."); save_settings(default_settings); return default_settings
    try:
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f: settings = json.load(f)
        if 'max_alerts_retention' not in settings or not isinstance(settings.get('max_alerts_retention'), int) or settings.get('max_alerts_retention') < 100 :
            logger.warning(f"Invalid 'max_alerts_retention' in {SETTINGS_FILE}. Resetting to default {DEFAULT_RETENTION}."); settings['max_alerts_retention'] = DEFAULT_RETENTION; save_settings(settings)
        return settings
    except Exception as e: logger.error(f"Error loading {SETTINGS_FILE}: {e}. Using defaults.", exc_info=True); return default_settings

def save_settings(settings):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f: json.dump(settings, f, indent=2); logger.info(f"Settings saved to {SETTINGS_FILE}"); return True
    except IOError as e: logger.error(f"Error saving settings to {SETTINGS_FILE}: {e}", exc_info=True); return False

current_settings = load_settings()
MAX_ALERTS_RETENTION = current_settings.get("max_alerts_retention", DEFAULT_RETENTION)
logger.info(f"Max Alerts Retention set to: {MAX_ALERTS_RETENTION}")

# --- Jalali Date Filter ---
def to_jalali(gregorian_dt_str, format='%Y/%m/%d', convert_to_jalali=True):
    if not gregorian_dt_str or not isinstance(gregorian_dt_str, str):
        return ""
    original_dt_str = gregorian_dt_str
    if gregorian_dt_str.endswith('Z'):
        gregorian_dt_str = gregorian_dt_str[:-1] + '+00:00'
    g_dt = None
    try:
        g_dt = dt.fromisoformat(gregorian_dt_str)
    except ValueError:
        possible_formats = [
            '%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S',
            '%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S',
        ]
        for fmt in possible_formats:
            try:
                g_dt = dt.strptime(gregorian_dt_str, fmt)
                break
            except ValueError:
                continue
        if not g_dt:
            logger.warning(f"Could not parse date string: {original_dt_str}")
            return original_dt_str
    try:
        if convert_to_jalali:
            j_dt = jdatetime.datetime.fromgregorian(datetime=g_dt)
            return j_dt.strftime(format)
        else:
            return g_dt.strftime(format)
    except Exception as e:
        logger.error(f"Error formatting date {original_dt_str}: {e}")
        return original_dt_str

app.jinja_env.filters['to_jalali'] = to_jalali

# --- Filtering and Sorting Logic ---
def filter_and_sort_alerts(alerts, search_term=None, severity_filter=None, group_filter=None, status_filter='active', sort_by='timestamp', order='desc'):
    filtered_alerts = alerts
    if status_filter == 'active': filtered_alerts = [a for a in filtered_alerts if a.get('alertmanager_status', 'firing') == 'firing']
    elif status_filter == 'resolved': filtered_alerts = [a for a in filtered_alerts if a.get('alertmanager_status') == 'resolved']
    if group_filter:
        if group_filter == 'Default': filtered_alerts = [a for a in filtered_alerts if not a.get('alert_group') or a.get('alert_group') == 'Default']
        else: filtered_alerts = [a for a in filtered_alerts if a.get('alert_group') == group_filter]
    if severity_filter: filtered_alerts = [a for a in filtered_alerts if a.get('severity', '').lower() == severity_filter.lower()]
    if search_term:
        search_term_lower = search_term.lower(); alerts_matching_search = []
        for a in filtered_alerts:
            match = False
            if (search_term_lower in a.get('message', '').lower() or search_term_lower in a.get('alertname', '').lower() or search_term_lower in a.get('alert_group', 'Default').lower() or search_term_lower in (a.get('acknowledge_comment') or '').lower()): match = True
            if not match and a.get('alertmanager_labels'):
                for k, v in a['alertmanager_labels'].items():
                    if search_term_lower in k.lower() or search_term_lower in str(v).lower(): match = True; break
            if not match and a.get('alertmanager_annotations'):
                 for k, v in a['alertmanager_annotations'].items():
                    if search_term_lower in k.lower() or search_term_lower in str(v).lower(): match = True; break
            if match: alerts_matching_search.append(a)
        filtered_alerts = alerts_matching_search
    reverse_order = (order == 'desc')
    severity_order = {'critical': 0, 'high': 1, 'warning': 2, 'medium': 3, 'info': 4, 'low': 5, 'debug': 6, 'unknown': 99}
    sort_key_func = None; default_sort_key = lambda x: x.get('timestamp', '')
    if sort_by == 'severity': sort_key_func = lambda x: (severity_order.get(x.get('severity', 'unknown').lower(), 99), x.get('timestamp', ''))
    elif sort_by == 'group': sort_key_func = lambda x: (x.get('alert_group', 'Default').lower(), x.get('timestamp', ''))
    elif sort_by == 'alertname': sort_key_func = lambda x: (x.get('alertname', '').lower(), x.get('timestamp', ''))
    elif sort_by == 'timestamp': sort_key_func = lambda x: x.get('timestamp', '')
    else: sort_key_func = default_sort_key; reverse_order = True
    try: filtered_alerts.sort(key=sort_key_func, reverse=reverse_order)
    except TypeError as e: logger.error(f"Sorting error: {e}. Falling back to timestamp.", exc_info=True); filtered_alerts.sort(key=default_sort_key, reverse=True)
    return filtered_alerts

# --- Core Routes ---
@app.route('/')
def index():
    """Displays the main Alert Bord."""
    search_term = request.args.get('search', '').strip()
    severity_filter = request.args.get('severity', '')
    group_filter = request.args.get('group', '')
    status_filter = request.args.get('status', 'active')
    sort_by = request.args.get('sort_by', 'timestamp')
    order = request.args.get('order', 'desc')

    all_alerts = read_alerts_from_db()
    available_groups = get_unique_groups_db()
    filtered_sorted_alerts = filter_and_sort_alerts(all_alerts, search_term, severity_filter, group_filter, status_filter, sort_by, order)
    current_retention_value = current_settings.get("max_alerts_retention", DEFAULT_RETENTION)
    severity_levels = ['critical', 'high', 'warning', 'medium', 'info', 'low', 'debug', 'unknown']
    sort_options = ['timestamp', 'group', 'severity', 'alertname']

    title_icon = 'fas fa-globe'
    if group_filter:
        gf_lower=group_filter.lower();
        if'db'in gf_lower:title_icon='fas fa-database'
        elif'net'in gf_lower:title_icon='fas fa-network-wired'
        elif'web'in gf_lower:title_icon='fas fa-window-maximize'
        elif'node'in gf_lower or'host'in gf_lower:title_icon='fas fa-server'
        elif'kube'in gf_lower:title_icon='fas fa-cubes'
        elif'default'in gf_lower:title_icon='fas fa-box'
        else:title_icon='fas fa-folder'

    return render_template('index.html', alerts=filtered_sorted_alerts, search_term=search_term,
                           severity_filter=severity_filter, group_filter=group_filter, status_filter=status_filter,
                           available_groups=available_groups, sort_by=sort_by, order=order, sort_options=sort_options,
                           severity_levels=severity_levels, current_retention=current_retention_value, title_icon=title_icon)

@app.route('/alert', methods=['POST']) # Webhook endpoint
def receive_alert():
    """Receives webhook alerts."""
    source_ip = request.remote_addr
    logger.info(f"Received webhook request on /alert from {source_ip}")
    webhook_received_counter.inc()

    raw_payload = None # برای لاگ کردن
    try:
        # ابتدا سعی کنید به عنوان JSON بخوانید
        webhook_payload = request.get_json(silent=True)
        if webhook_payload:
             raw_payload = json.dumps(webhook_payload, indent=2) # برای لاگ خوانا
        else:
            # اگر JSON نبود، داده خام را بخوانید (ممکن است فرمت دیگری باشد)
             raw_data = request.get_data(as_text=True)
             raw_payload = raw_data
             logger.warning(f"Received non-JSON or empty payload from {source_ip}. Raw data: {raw_data[:500]}...") # لاگ بخشی از داده خام
             webhook_errors_counter.labels(reason='empty_or_invalid_payload').inc()
             return jsonify({"status": "error", "message": "Empty or invalid JSON payload"}), 400

        # لاگ کردن کامل payload دریافت شده (برای دیباگ)
        logger.debug(f"Full webhook payload from {source_ip}:\n{raw_payload}")

        # بررسی ساختار مورد انتظار Alertmanager
        if 'alerts' not in webhook_payload or not isinstance(webhook_payload['alerts'], list):
             logger.warning(f"Invalid Alertmanager payload format from {source_ip}. Missing 'alerts' list or not a list.")
             webhook_errors_counter.labels(reason='invalid_format').inc()
             return jsonify({"status": "error", "message": "Invalid payload format: 'alerts' list missing or not a list."}), 400

        now_iso = dt.now().isoformat()
        group_labels = webhook_payload.get('groupLabels', {})
        common_labels = webhook_payload.get('commonLabels', {})
        common_annotations = webhook_payload.get('commonAnnotations', {})
        logger.debug(f"CommonLabels: {common_labels}, CommonAnnotations: {common_annotations}, GroupLabels: {group_labels}")

        alerts_processed, alerts_updated, alerts_added, alerts_skipped = 0, 0, 0, 0

        # پردازش تک تک آلرت‌ها در payload
        for i, alert_payload in enumerate(webhook_payload['alerts']):
            logger.info(f"Processing alert #{i+1} from payload...")
            logger.debug(f"Alert payload #{i+1}: {json.dumps(alert_payload)}") # لاگ خود آلرت

            try: # <-- اضافه کردن try برای هر آلرت
                status = alert_payload.get('status', 'firing').lower()
                alert_labels = {**common_labels, **alert_payload.get('labels', {})}
                annotations = {**common_annotations, **alert_payload.get('annotations', {})}
                logger.debug(f"Alert #{i+1} - Status: {status}, Labels: {alert_labels}, Annotations: {annotations}")

                if status == 'firing':
                    severity = alert_labels.get('severity', 'unknown').lower()
                    message = annotations.get('description', annotations.get('summary', 'No description/summary'))
                    alertname = alert_labels.get('alertname', '')
                    if alertname and f"[{alertname}]" not in message and alertname not in message: message = f"[{alertname}] {message}"

                    # منطق تعیین گروه (بدون تغییر)
                    group_keys_priority = ['job', 'alertgroup', 'namespace', 'cluster', 'instance']; alert_group = 'Default'
                    for key in group_keys_priority:
                        if key in alert_labels and alert_labels[key]: alert_group = alert_labels[key]; break
                    if alert_group == 'Default' and alertname: alert_group = alertname
                    if not isinstance(alert_group, str) or not alert_group.strip(): alert_group = 'Default'
                    alert_group = alert_group.strip()

                    alert_data = { 'id': str(uuid.uuid4()), 'timestamp': now_iso, 'severity': severity, 'alert_group': alert_group, 'alertname': alertname, 'message': message, 'source_ip': source_ip, 'alertmanager_status': 'firing', 'resolved_at': None, 'alertmanager_labels': alert_labels, 'alertmanager_annotations': annotations, 'alertmanager_groupLabels': group_labels }
                    logger.debug(f"Alert #{i+1} - Prepared data for DB insert: {alert_data}")

                    if insert_alert_to_db(alert_data):
                        alerts_added += 1
                        logger.info(f"Alert #{i+1} (ID: {alert_data['id']}) successfully inserted.")
                    else:
                        alerts_skipped += 1
                        logger.warning(f"Alert #{i+1} skipped (likely duplicate or DB error during insert).")

                elif status == 'resolved':
                    logger.info(f"Alert #{i+1} is resolved. Searching for matching firing alert...")
                    matching_alert = find_matching_firing_alert_db(alert_labels)
                    if matching_alert:
                        logger.info(f"Found match for resolved alert: ID {matching_alert['id']}. Updating status...")
                        update_data = {'alertmanager_status': 'resolved', 'resolved_at': now_iso}
                        if update_alert_in_db(matching_alert['id'], update_data):
                            alerts_updated += 1
                            logger.info(f"Successfully updated alert ID {matching_alert['id']} to resolved.")
                        else:
                            alerts_skipped += 1
                            logger.error(f"Failed to update resolved status for alert ID {matching_alert['id']} in DB.")
                    else:
                        alerts_skipped += 1
                        logger.warning(f"No matching firing alert found in DB for resolved alert #{i+1} with labels: {alert_labels}")
                else:
                    alerts_skipped += 1
                    logger.warning(f"Alert #{i+1} has unknown status '{status}'. Skipping.")

                alerts_processed += 1

            except Exception as e_inner: # <-- گرفتن خطای پردازش هر آلرت
                alerts_skipped += 1
                alerts_processed += 1 # پردازش شده اما با خطا
                logger.error(f"Error processing alert #{i+1} inside the loop: {e_inner}", exc_info=True)
                # ادامه حلقه برای پردازش آلرت‌های بعدی

        # Pruning after processing all alerts in the batch
        if alerts_added > 0:
            logger.info("Pruning database after adding new alerts...")
            try: prune_alerts_db()
            except Exception as prune_e: logger.error(f"Error during pruning: {prune_e}", exc_info=True)

        response_message = f"Processed {alerts_processed} alerts. Added: {alerts_added}, Updated(Resolved): {alerts_updated}, Skipped: {alerts_skipped}"
        logger.info(f"Webhook processing complete for {source_ip}. Result: {response_message}")
        return jsonify({ "status": "success", "message": response_message }), 200

    except Exception as e_outer: # <-- گرفتن خطای کلی پردازش وب‌هوک
        logger.error(f"Fatal error processing webhook from {source_ip}: {e_outer}", exc_info=True)
        webhook_errors_counter.labels(reason='exception').inc()
        # در صورت خطا، payload خام را لاگ کنید اگر قبلاً JSON نبوده
        if not webhook_payload and raw_payload:
             logger.error(f"Raw data that caused error: {raw_payload[:1000]}") # لاگ بخش بزرگتری از داده خام در صورت خطا
        return jsonify({"status": "error", "message": "Internal server error processing webhook."}), 500
    
# --- API Routes ---
@app.route('/api/new_alerts')
def api_new_alerts():
    since_timestamp_str = request.args.get('since_timestamp', None); logger.debug(f"/api/new_alerts?since={since_timestamp_str}")
    if not since_timestamp_str: return jsonify([])
    new_alerts = []
    try: db = get_db(); cursor = db.execute("SELECT * FROM alerts WHERE timestamp > ? AND alertmanager_status = 'firing' ORDER BY timestamp DESC LIMIT 50", (since_timestamp_str,)); new_alerts = [row_to_dict(row) for row in cursor.fetchall()]
    except Exception as e: logger.error(f"Error querying new alerts: {e}", exc_info=True); return jsonify({"error": "ISE"}), 500
    logger.debug(f"Found {len(new_alerts)} new alerts since {since_timestamp_str}."); return jsonify(new_alerts)

@app.route('/settings', methods=['POST'])
def update_settings():
    """API endpoint to update settings (no login required)."""
    global current_settings, MAX_ALERTS_RETENTION
    try: # <-- شروع بلوک try خارجی
        data = request.get_json(silent=True)
        if not data:
            logger.warning("Update settings: Invalid/empty JSON")
            return jsonify({"status": "error", "message": "Invalid or empty JSON request data."}), 400

        new_retention_str = data.get('max_alerts_retention')
        new_retention = None # مقدار اولیه

        # --- بلوک try...except داخلی برای اعتبارسنجی ---
        try:
            new_retention = int(new_retention_str)
            if new_retention < 100:
                raise ValueError("Retention value must be an integer >= 100.")
        except (ValueError, TypeError) as validation_error: # <-- except مربوط به try داخلی
            logger.warning(f"Update settings: Invalid retention value: '{new_retention_str}'. Error: {validation_error}")
            error_message = str(validation_error) if isinstance(validation_error, ValueError) else "Invalid retention value type provided."
            return jsonify({"status": "error", "message": error_message}), 400
        # --- پایان بلوک try...except داخلی ---

        # اگر اعتبارسنجی موفق بود، ادامه می‌دهیم
        settings_to_save = load_settings()
        settings_to_save['max_alerts_retention'] = new_retention

        if save_settings(settings_to_save):
            current_settings = settings_to_save
            MAX_ALERTS_RETENTION = new_retention
            logger.info(f"Retention updated to {new_retention}")
            # flash() removed - inappropriate for API endpoint returning JSON
            return jsonify({"status": "success", "message": f"Retention set to {new_retention}."}), 200
        else:
            logger.error("Failed to save settings file.")
            return jsonify({"status": "error", "message": "Failed to write settings file."}), 500

    except Exception as e: # <-- except مربوط به try خارجی
        logger.error(f"Error updating settings: {e}", exc_info=True)
        return jsonify({"status": "error", "message": "Internal server error updating settings."}), 500


@app.route('/api/alert/<string:alert_id>/acknowledge', methods=['POST'])
def acknowledge_alert(alert_id):
    """API endpoint to acknowledge an alert (no login required)."""
    acknowledge_requests_counter.inc()
    if not alert_id:
        acknowledge_errors_counter.labels(reason='missing_id').inc()
        logger.warning("Acknowledge request received without alert_id.")
        return jsonify({"status": "error", "message": "Alert ID missing."}), 400

    comment = None
    try:
        data = request.get_json(silent=True)
        if data and 'comment' in data and isinstance(data['comment'], str):
            comment = data['comment'].strip()[:500] # Limit comment length
    except Exception as e:
        logger.warning(f"Could not parse JSON for ack comment ID {alert_id}: {e}")

    # In simplified mode, user is always considered "System" or similar
    acknowledged_by_user = "System"
    logger.info(f"Acknowledge request: ID={alert_id}, User='{acknowledged_by_user}', Comment='{comment}'")

    alert = find_alert_by_id(alert_id)

    # Validations
    if not alert:
        acknowledge_errors_counter.labels(reason='not_found').inc()
        logger.warning(f"Acknowledge failed: Alert ID {alert_id} not found.")
        return jsonify({"status": "error", "message": "Alert not found."}), 404

    if alert.get('alertmanager_status') == 'resolved':
        acknowledge_errors_counter.labels(reason='already_resolved').inc()
        logger.warning(f"Acknowledge failed: Alert ID {alert_id} is already resolved.")
        return jsonify({"status": "error", "message": "Cannot acknowledge an already resolved alert."}), 400

    # Check if already acknowledged (idempotency)
    if alert.get('acknowledged'):
        logger.info(f"Alert ID {alert_id} was already acknowledged by '{alert.get('acknowledged_by')}' at {alert.get('acknowledged_at')}. Returning existing info.")
        return jsonify({
            "status": "success",
            "message": "Already acknowledged.",
            "acknowledged_by": alert.get('acknowledged_by'),
            "acknowledged_at": alert.get('acknowledged_at'),
            "acknowledge_comment": alert.get('acknowledge_comment')
        }), 200

    # Perform Acknowledgment
    now_iso = dt.now().isoformat()
    update_data = {
        'acknowledged': 1,
        'acknowledged_by': acknowledged_by_user, # Set consistent value
        'acknowledged_at': now_iso,
        'acknowledge_comment': comment
    }

    # Update DB
    if update_alert_in_db(alert_id, update_data):
        logger.info(f"Acknowledge successful for alert ID {alert_id} by '{acknowledged_by_user}'.")
        return jsonify({
            "status": "success",
            "message": "Alert acknowledged successfully.",
            "acknowledged_by": acknowledged_by_user,
            "acknowledged_at": now_iso,
            "acknowledge_comment": comment
        }), 200
    else:
        logger.error(f"Acknowledge failed: Database update error for alert ID {alert_id}.")
        acknowledge_errors_counter.labels(reason='db_update_failed').inc()
        return jsonify({"status": "error", "message": "Failed to update alert acknowledgment status in database."}), 500

@app.route('/export/<string:format>', methods=['GET'])
def export_data(format):
    search_term = request.args.get('search', '').strip(); severity_filter = request.args.get('severity', ''); group_filter = request.args.get('group', ''); status_filter = request.args.get('status', 'all'); sort_by = request.args.get('sort_by', 'timestamp'); order = request.args.get('order', 'desc')
    logger.info(f"Export request: format={format}, status={status_filter}, group={group_filter}, sev={severity_filter}, search='{search_term}'")
    all_alerts = read_alerts_from_db(); filtered_sorted_alerts = filter_and_sort_alerts(all_alerts, search_term, severity_filter, group_filter, status_filter, sort_by, order)
    if not filtered_sorted_alerts: flash("No data matched filters for export.", "warning"); return Response("No data.", mimetype='text/plain', status=404)
    export_timestamp = dt.now().strftime('%Y%m%d_%H%M%S'); filename_parts = ["alerts_export", export_timestamp];
    if status_filter != 'all': filename_parts.append(f"status-{status_filter}")
    if group_filter: filename_parts.append(f"group-{group_filter.replace(' ','_')}")
    if severity_filter: filename_parts.append(f"severity-{severity_filter}")
    if search_term: filename_parts.append("search-active")
    filename_base = "_".join(filename_parts)
    if format == 'json':
        filename = f"{filename_base}.json"; logger.info(f"Exporting {len(filtered_sorted_alerts)} to JSON: {filename}"); export_list = []
        for alert in filtered_sorted_alerts: alert_copy = alert.copy(); alert_copy['timestamp_jalali'] = to_jalali(alert_copy.get('timestamp'), format='%Y/%m/%d %H:%M:%S'); alert_copy['resolved_at_jalali'] = to_jalali(alert_copy.get('resolved_at'), format='%Y/%m/%d %H:%M:%S'); alert_copy['acknowledged_at_jalali'] = to_jalali(alert_copy.get('acknowledged_at'), format='%Y/%m/%d %H:%M:%S'); export_list.append(alert_copy)
        json_data = json.dumps(export_list, indent=2, ensure_ascii=False); return Response(json_data, mimetype='application/json', headers={'Content-Disposition': f'attachment;filename="{filename}"', 'Content-Type': 'application/json; charset=utf-8'})
    elif format == 'csv':
        filename = f"{filename_base}.csv"; logger.info(f"Exporting {len(filtered_sorted_alerts)} to CSV: {filename}"); si = io.StringIO(); fieldnames = ['id', 'timestamp', 'timestamp_jalali', 'alert_group', 'alertname', 'severity', 'message', 'alertmanager_status', 'resolved_at', 'resolved_at_jalali', 'acknowledged', 'acknowledged_by', 'acknowledged_at', 'acknowledged_at_jalali', 'acknowledge_comment', 'source_ip', 'alertmanager_labels_str', 'alertmanager_annotations_str']
        writer = csv.DictWriter(si, fieldnames=fieldnames, extrasaction='ignore', quoting=csv.QUOTE_ALL); writer.writeheader(); rows_to_write = []
        for alert in filtered_sorted_alerts: row = alert.copy(); row['timestamp_jalali'] = to_jalali(row.get('timestamp'), format='%Y/%m/%d %H:%M:%S'); row['resolved_at_jalali'] = to_jalali(row.get('resolved_at'), format='%Y/%m/%d %H:%M:%S'); row['acknowledged_at_jalali'] = to_jalali(row.get('acknowledged_at'), format='%Y/%m/%d %H:%M:%S'); row['alertmanager_labels_str'] = json.dumps(row.get('alertmanager_labels', {}), ensure_ascii=False); row['alertmanager_annotations_str'] = json.dumps(row.get('alertmanager_annotations', {}), ensure_ascii=False); rows_to_write.append(row)
        writer.writerows(rows_to_write); csv_data = si.getvalue().encode('utf-8-sig'); return Response(csv_data, mimetype='text/csv', headers={'Content-Disposition': f'attachment;filename="{filename}"', 'Content-Type': 'text/csv; charset=utf-8-sig'})
    else: logger.warning(f"Invalid export format: {format}"); flash(f"Invalid format: {format}", "danger"); return redirect(request.referrer or url_for('index'))

# --- Prometheus Metrics Update Function ---
def update_alert_metrics():
    logger.info("Starting Prometheus metrics update thread...")
    while True:
        counts = None; conn = None
        try:
            conn = sqlite3.connect(DATABASE, detect_types=sqlite3.PARSE_DECLTYPES); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) as total, COALESCE(SUM(CASE WHEN alertmanager_status = 'firing' AND acknowledged = 0 THEN 1 ELSE 0 END), 0) as firing_unacked, COALESCE(SUM(CASE WHEN alertmanager_status = 'firing' AND acknowledged = 1 THEN 1 ELSE 0 END), 0) as firing_acked, COALESCE(SUM(CASE WHEN alertmanager_status = 'resolved' THEN 1 ELSE 0 END), 0) as resolved FROM alerts")
            counts = cursor.fetchone(); conn.close()
            if counts:
                with metrics_lock:
                    alerts_total_gauge.set(counts['total']); alerts_firing_unacked_gauge.set(counts['firing_unacked']); alerts_firing_acked_gauge.set(counts['firing_acked']); alerts_resolved_gauge.set(counts['resolved'])
                logger.debug(f"Updated Prometheus metrics: Total={counts['total']}, FiringUnack={counts['firing_unacked']}, FiringAck={counts['firing_acked']}, Resolved={counts['resolved']}")
            else: logger.warning("Metrics Thread: Failed to retrieve counts.")
        except Exception as e: logger.error(f"Metrics Thread: Error: {e}", exc_info=True)
        finally:
            if conn:
                try: conn.close()
                except Exception as close_e: logger.error(f"Metrics Thread: Error closing DB: {close_e}")
        time.sleep(30)

# --- Main Execution ---
if __name__ == '__main__':
    logger.info(f"Starting Alert Bord application (v9.1 - Simplified)...") # Updated Name
    logger.info(f"Database file: {DATABASE}")
    logger.info(f"Settings file: {SETTINGS_FILE}")
    logger.info(f"Initial Max Alerts Retention: {MAX_ALERTS_RETENTION}")
    if len(app.secret_key) < 16: logger.warning("FLASK_SECRET_KEY is short or temporary.")

    with app.app_context():
        logger.info("Initializing database schema if needed...")
        init_db(app)
        logger.info("Performing initial alert pruning...")
        prune_alerts_db()

    metrics_thread = threading.Thread(target=update_alert_metrics, daemon=True)
    metrics_thread.start()

    try:
        from waitress import serve
        host='0.0.0.0'; port=5000; threads=8;
        logger.info(f"Starting Waitress server on http://{host}:{port} with {threads} threads.")
        logger.info(f"Prometheus metrics available at http://{host}:{port}/metrics")
        serve(app, host=host, port=port, threads=threads)
    except ImportError:
        logger.warning("Waitress not found. Falling back to Flask development server (NOT RECOMMENDED FOR PRODUCTION).")
        app.run(debug=False, host='0.0.0.0', port=5000)