"""
Flask routes for the CanPi application.
"""

from flask import (
    Blueprint, request, jsonify, current_app, send_file, abort,
    render_template, redirect, url_for, flash, g, make_response, Response
)
from functools import wraps
import csv
import io
import json
import os
import time
from datetime import datetime

from .models import (
    get_db, create_session, get_session, delete_session, get_api_key,
    hash_password, verify_password, create_user, get_user_by_username,
    get_user_by_id, get_all_users, delete_user, update_user, change_password,
    create_api_key, get_all_api_keys, revoke_api_key,
)
from .can_interface import capture_manager
from .can_decoder import can_decoder
from .utils import USBManager, pamas_manager

bp = Blueprint('main', __name__)

# ── Auth helpers ─────────────────────────────────────────────────────

def _load_user():
    """Load the current user from session cookie into g."""
    if hasattr(g, '_user_loaded'):
        return
    g._user_loaded = True
    token = request.cookies.get('session_token')
    if not token:
        # Also check Authorization header (for API key access)
        auth = request.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            api_key_row = get_api_key(auth[7:])
            if api_key_row:
                g.user_id = api_key_row['created_by']
                g.username = 'api'
                g.user_role = 'user'
                g.allowed_canifs = api_key_row['can_access']
                g.is_api = True
                return
        return
    row = get_session(token)
    if row and row['active']:
        g.user_id = row['user_id']
        g.username = row['username']
        g.user_role = row['role']
        g.allowed_canifs = row['can_access']
        g.session_token = token
        g.selected_interface = row['selected_interface']


@bp.before_request
def before_request():
    _load_user()


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not hasattr(g, 'user_id'):
            if request.is_json or getattr(g, 'is_api', False):
                abort(401, description='Authentication required')
            return redirect(url_for('main.login'))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not hasattr(g, 'user_id'):
            if request.is_json:
                abort(401, description='Authentication required')
            return redirect(url_for('main.login'))
        if g.user_role != 'admin':
            abort(403, description='Admin access required')
        return fn(*args, **kwargs)
    return wrapper


# ── Pages ────────────────────────────────────────────────────────────

@bp.route('/')
def index():
    if hasattr(g, 'user_id'):
        return redirect(url_for('main.dashboard'))
    return redirect(url_for('main.login'))


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if hasattr(g, 'user_id'):
        return redirect(url_for('main.dashboard'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = get_user_by_username(username)
        if user and user['active'] and verify_password(password, user['password_hash']):
            token = create_session(user['id'], user['can_access'])
            resp = make_response(redirect(url_for('main.dashboard')))
            resp.set_cookie('session_token', token, httponly=True,
                            samesite='Lax', max_age=86400 * 365)
            return resp
        flash('Invalid username or password', 'danger')
    return render_template('login.html')


@bp.route('/logout')
def logout():
    token = request.cookies.get('session_token')
    if token:
        delete_session(token)
    resp = make_response(redirect(url_for('main.login')))
    resp.delete_cookie('session_token')
    return resp


@bp.route('/dashboard')
@login_required
def dashboard():
    interfaces = g.allowed_canifs.split(',') if hasattr(g, 'allowed_canifs') else []
    return render_template('dashboard.html',
                           username=g.username, role=g.user_role,
                           interfaces=interfaces)


# ── Admin ────────────────────────────────────────────────────────────

@bp.route('/admin')
@admin_required
def admin():
    users = get_all_users()
    api_keys = get_all_api_keys()
    pamas_running = pamas_manager.is_running
    pamas_data = pamas_manager.get_telemetry() if pamas_running else []
    return render_template('admin_dashboard.html',
                           users=users, api_keys=api_keys,
                           pamas_running=pamas_running, pamas_data=pamas_data)


@bp.route('/admin/create_user', methods=['GET', 'POST'])
@admin_required
def create_user_route():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        role = request.form.get('role', 'user')
        can_ifs = request.form.getlist('can_access')
        can_access = ','.join(can_ifs) if can_ifs else 'can9'

        if not username or not password:
            flash('Username and password are required', 'danger')
            return render_template('admin_create_user.html')

        if role not in ('admin', 'user'):
            role = 'user'

        try:
            create_user(username, password, role, can_access)
            flash(f'User "{username}" created', 'success')
            return redirect(url_for('main.admin'))
        except Exception as e:
            flash(f'Error creating user: {e}', 'danger')

    return render_template('admin_create_user.html')


@bp.route('/admin/users')
@admin_required
def list_users():
    return jsonify(get_all_users())


@bp.route('/admin/users/<int:user_id>', methods=['POST'])
@admin_required
def edit_user(user_id):
    action = request.form.get('action')
    if action == 'delete':
        if user_id == g.user_id:
            flash('Cannot delete yourself', 'danger')
        else:
            delete_user(user_id)
            flash('User deleted', 'success')
    elif action == 'update':
        role = request.form.get('role', 'user')
        can_ifs = request.form.getlist('can_access')
        can_access = ','.join(can_ifs) if can_ifs else 'can9'
        active = 1 if request.form.get('active') else 0
        update_user(user_id, role=role, can_access=can_access, active=active)
        flash('User updated', 'success')
    elif action == 'change_password':
        new_pw = request.form.get('new_password', '')
        if new_pw:
            change_password(user_id, new_pw)
            flash('Password changed', 'success')
    return redirect(url_for('main.admin'))


# ── CAN Control ──────────────────────────────────────────────────────

@bp.route('/can/start_capture', methods=['POST'])
@login_required
def start_capture():
    data = request.get_json(silent=True) or {}
    requested = data.get('interfaces', [])
    if isinstance(requested, str):
        requested = [x.strip() for x in requested.split(',')]

    allowed = g.allowed_canifs.split(',') if hasattr(g, 'allowed_canifs') else []
    for iface in requested:
        if iface not in allowed:
            abort(403, description=f'Interface {iface} not allowed')

    if not requested:
        requested = allowed

    session_id = getattr(g, 'session_token', g.username)
    capture_manager.start_session(session_id, requested)
    return jsonify({'status': 'capture started', 'interfaces': requested}), 200


@bp.route('/can/stop_capture', methods=['POST'])
@login_required
def stop_capture():
    session_id = getattr(g, 'session_token', g.username)
    sess = capture_manager.stop_session(session_id)
    if not sess:
        return jsonify({'status': 'no active capture'}), 200
    return jsonify({'status': 'capture stopped', 'frames': len(sess.get_buffer())}), 200


@bp.route('/can/stream')
@login_required
def can_stream():
    """SSE endpoint streaming real-time CAN frames with decoded values."""
    def generate():
        q = capture_manager.global_queue
        while True:
            try:
                frame = q.get(timeout=30)
                # Decode the frame and attach decoded signals
                decoded = can_decoder.decode_frame(frame)
                if decoded:
                    frame['decoded'] = decoded
                yield f"data: {json.dumps(frame)}\n\n"
            except Exception:
                yield ": keepalive\n\n"

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache',
                             'X-Accel-Buffering': 'no'})


# ── CSV Export ───────────────────────────────────────────────────────

@bp.route('/capture/export')
@login_required
def export_capture():
    session_id = getattr(g, 'session_token', g.username)
    sess = capture_manager.get_session(session_id)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['timestamp', 'interface', 'arbitration_id', 'dlc', 'data', 'is_fd'])

    if sess:
        for frame in sess.get_buffer():
            writer.writerow([
                frame.get('timestamp', ''),
                frame.get('interface', ''),
                frame.get('arbitration_id', ''),
                frame.get('dlc', ''),
                frame.get('data', ''),
                frame.get('is_fd', False),
            ])

    output.seek(0)
    ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    return send_file(
        io.BytesIO(output.getvalue().encode()),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'can_capture_{ts}.csv'
    )


# ── API Key Management ──────────────────────────────────────────────

@bp.route('/admin/api_keys', methods=['GET', 'POST'])
@admin_required
def api_keys_page():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        can_ifs = request.form.getlist('can_access')
        can_access = ','.join(can_ifs) if can_ifs else 'can0,can1,can9'

        if not name:
            flash('Key name is required', 'danger')
            return redirect(url_for('main.admin'))

        raw_key = create_api_key(name, can_access, g.user_id)
        flash(f'API key created. Save it now — it won\'t be shown again: {raw_key}', 'warning')
        return redirect(url_for('main.admin'))

    return jsonify(get_all_api_keys())


@bp.route('/admin/api_keys/<int:key_id>/revoke', methods=['POST'])
@admin_required
def revoke_key(key_id):
    revoke_api_key(key_id)
    flash('API key revoked', 'success')
    return redirect(url_for('main.admin'))


# ── REST API (token/key auth) ───────────────────────────────────────

@bp.route('/api/v1/can/<interface>')
@login_required
def api_can_stream(interface):
    """REST API SSE stream for a specific CAN interface."""
    allowed = g.allowed_canifs.split(',') if hasattr(g, 'allowed_canifs') else []
    if interface not in allowed:
        abort(403, description=f'Interface {interface} not allowed')

    session_id = f"api_{g.username}_{interface}"
    capture_manager.start_session(session_id, [interface])

    def generate():
        q = capture_manager.global_queue
        while True:
            try:
                frame = q.get(timeout=30)
                if frame.get('interface') == interface:
                    yield f"data: {json.dumps(frame)}\n\n"
            except Exception:
                yield ": keepalive\n\n"

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache',
                             'X-Accel-Buffering': 'no'})


# ── PAMAS S50P ───────────────────────────────────────────────────────

@bp.route('/pamas/ports')
@admin_required
def pamas_ports():
    """Detect available serial/TTY devices on the system."""
    return jsonify(pamas_manager.scan_ports())


@bp.route('/pamas/status')
@login_required
def pamas_status():
    """Return current PAMAS manager status (auto-mode, ports, etc)."""
    return jsonify(pamas_manager.get_status())


@bp.route('/pamas/start', methods=['POST'])
@admin_required
def start_pamas():
    data = request.get_json(silent=True) or {}
    ports = data.get('ports')  # list of serial ports, or None for simulation
    pamas_manager.start(ports)
    mode = 'simulation' if not ports else 'real'
    return jsonify({'status': f'PAMAS monitoring started ({mode})'}), 200


@bp.route('/pamas/stop', methods=['POST'])
@admin_required
def stop_pamas():
    """Stop manual override and return to auto-detect mode."""
    pamas_manager.stop()
    return jsonify({'status': 'PAMAS stopped — auto-detect re-enabled'}), 200


@bp.route('/pamas/telemetry')
@login_required
def pamas_telemetry():
    return jsonify(pamas_manager.get_telemetry())


# ── USB Drive Management ─────────────────────────────────────────────

@bp.route('/usb/drives')
@login_required
def usb_drives():
    return jsonify(USBManager.detect_drives())


@bp.route('/usb/log', methods=['POST'])
@login_required
def usb_log():
    """Write current capture buffer to a CSV on a USB drive."""
    data = request.get_json(silent=True) or {}
    drive_path = data.get('drive_path', USBManager.MOUNT_BASE)

    if not USBManager.is_writable(drive_path):
        abort(400, description='Drive is not writable or not under allowed mount point')

    session_id = getattr(g, 'session_token', g.username)
    sess = capture_manager.get_session(session_id)
    if not sess:
        abort(400, description='No active capture session')

    log_dir = USBManager.get_log_path(drive_path)
    ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    filepath = f"{log_dir}/canpi_{ts}.csv"

    with open(filepath, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['timestamp', 'interface', 'arbitration_id', 'dlc', 'data', 'is_fd'])
        for frame in sess.get_buffer():
            writer.writerow([
                frame.get('timestamp', ''),
                frame.get('interface', ''),
                frame.get('arbitration_id', ''),
                frame.get('dlc', ''),
                frame.get('data', ''),
                frame.get('is_fd', False),
            ])

    return jsonify({'status': 'logged', 'file': filepath,
                    'frames': len(sess.get_buffer())}), 200


# ── Decoded CAN Data ─────────────────────────────────────────────────

@bp.route('/can/decoded')
@login_required
def decoded_values():
    """Return all currently decoded CAN signal values."""
    return jsonify(can_decoder.get_all_values())


@bp.route('/can/decoded/categories')
@login_required
def decoded_categories():
    """Return decoded values grouped by category."""
    return jsonify(can_decoder.get_values_by_category())


@bp.route('/can/decoded/history/<key>')
@login_required
def decoded_history(key):
    """Return history for a specific signal (e.g., rpm, speed)."""
    return jsonify(can_decoder.get_history(key))


@bp.route('/can/stats')
@login_required
def bus_stats():
    """Return CAN bus health statistics."""
    return jsonify(can_decoder.get_bus_stats())


@bp.route('/can/signals')
@login_required
def signal_definitions():
    """Return all known signal definitions for the UI."""
    return jsonify(can_decoder.get_signal_definitions())


# ── Health ───────────────────────────────────────────────────────────

@bp.route('/health')
def health():
    return jsonify({
        'status': 'ok',
        'active_captures': capture_manager.active_count(),
        'pamas_running': pamas_manager.is_running,
    })