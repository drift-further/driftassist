"""Authenticated, fixed-purpose update status and background actions."""
from flask import Blueprint, jsonify, request
from shared.updates import manager, UpdateError

updates_bp = Blueprint('updates_bp', __name__)


@updates_bp.get('/api/update')
def update_status():
    return jsonify(manager().snapshot())


@updates_bp.post('/api/update/check')
def update_check():
    try:
        manager().start('check')
        return jsonify(ok=True, **manager().snapshot()), 202
    except UpdateError as exc:
        return jsonify(ok=False, error=str(exc)), 409


@updates_bp.post('/api/update')
def update_apply():
    data = request.get_json(silent=True)
    status = manager().snapshot()
    expected = data.get('expected_commit') if isinstance(data, dict) else None
    if (not expected or expected != (status.get('installed_commit') if status.get('phase') == 'repair_required' else status.get('latest_commit'))
            or not (status.get('available') or status.get('phase') == 'repair_required') or status.get('blocked_reason')):
        return jsonify(ok=False, error='Check updates and resolve local changes before installing the reviewed version.'), 409
    try:
        manager().start('apply', expected)
        return jsonify(ok=True, **manager().snapshot()), 202
    except UpdateError as exc:
        return jsonify(ok=False, error=str(exc)), 409
