import os
import logging
from datetime import datetime, timezone
import jwt
from flask import Blueprint, request, jsonify
from google.cloud import firestore
from google.cloud.firestore import FieldFilter

# Set up logging
logger = logging.getLogger(__name__)

bp = Blueprint('presets', __name__)

JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY')

_db = None

def get_db():
    global _db
    if _db is None:
        _db = firestore.Client()
    return _db

def authenticate_request():
    """
    Extracts and decodes the bearer token from the Authorization header.
    Returns:
        tuple: (decoded_payload, error_response_dict, status_code)
        If successful, error_response_dict is None.
    """
    # Allow local Dev Admin Bypass first (only in development environment)
    dev_bypass = request.headers.get('X-Dev-Bypass-Admin')
    if dev_bypass == 'true' and os.environ.get('FLASK_ENV') == 'development':
        # Return a mocked admin payload
        return {
            'sub': '12345',
            'username': 'Dev Admin',
            'name': 'Dev Admin',
            'isAdmin': True,
            'is_admin': True,
            'isSuperadmin': True
        }, None, 200

    auth_header = request.headers.get('Authorization')
    if not auth_header:
        logger.error("[PRESETS ERROR] Authorization header is missing.")
        return None, {"error": "Unauthorized", "details": "Authorization header missing"}, 401
    
    if not auth_header.startswith('Bearer '):
        logger.error("[PRESETS ERROR] Authorization header is not a Bearer token.")
        return None, {"error": "Unauthorized", "details": "Authorization header must start with Bearer"}, 401
    
    parts = auth_header.split(" ")
    if len(parts) != 2:
        logger.error("[PRESETS ERROR] Authorization header has invalid format.")
        return None, {"error": "Unauthorized", "details": "Invalid Authorization header format"}, 401
        
    token = parts[1]
    
    if not JWT_SECRET_KEY:
        logger.error("[PRESETS ERROR] JWT_SECRET_KEY is not configured on the server.")
        return None, {"error": "Server error", "details": "JWT_SECRET_KEY is not configured on the server"}, 500
        
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=['HS256'])
        return payload, None, 200
    except jwt.ExpiredSignatureError:
        logger.error("[PRESETS ERROR] The authentication token has expired.")
        return None, {"error": "Unauthorized", "details": "Token has expired"}, 401
    except jwt.InvalidTokenError as e:
        logger.error(f"[PRESETS ERROR] Invalid token: {e}")
        return None, {"error": "Unauthorized", "details": "Invalid token"}, 401


@bp.route('/presets', methods=['GET'])
@bp.route('/api/v1/presets', methods=['GET'])
def get_official_presets():
    """
    ENDPOINT 1: GET /presets (PUBLIC)
    Filter the 'presets' collection for documents where 'official' is set to True.
    """
    try:
        presets_ref = get_db().collection('presets')
        query = (
            presets_ref.where(filter=FieldFilter('official', '==', True))
            .stream()
        )

        output = []
        for doc in query:
            data = doc.to_dict()
            data['id'] = doc.id
            output.append(data)
        return jsonify(output), 200
    except Exception as e:
        logger.error(f"[PRESETS ERROR] Failed to fetch official presets: {e}")
        return jsonify({"error": "Internal Server Error", "details": str(e)}), 500


@bp.route('/api/v1/user-presets', methods=['GET'])
def get_user_presets():
    """
    ENDPOINT 2: GET /api/v1/user-presets (AUTHENTICATED)
    Fetches a user's unique configurations based on their authenticated token session.
    Supports streaming all presets for administrators who pass all=true.
    """
    payload, err_resp, status = authenticate_request()
    if err_resp:
        return jsonify(err_resp), status
        
    discord_id = payload.get('sub')
    if not discord_id:
        logger.error("[PRESETS ERROR] Token decoded successfully but 'sub' claim is missing.")
        return jsonify({"error": "Unauthorized", "details": "Token missing sub claim"}), 401
        
    is_admin = payload.get('isAdmin', False) or payload.get('is_admin', False) or payload.get('isSuperadmin', False)
    
    try:
        presets_ref = get_db().collection('presets')
        
        # Admin bypass to load all database records (e.g. for the Admin dashboard)
        if is_admin and request.args.get('all') == 'true':
            query = presets_ref.stream()
        else:
            query = (
                presets_ref.where(
                    filter=FieldFilter('creator_id', '==', discord_id)
                )
                .stream()
            )
        
        output = []
        for doc in query:
            data = doc.to_dict()
            data['id'] = doc.id
            output.append(data)
        return jsonify(output), 200
    except Exception as e:
        logger.error(f"[PRESETS ERROR] Failed to fetch user presets for creator_id {discord_id}: {e}")
        return jsonify({"error": "Internal Server Error", "details": str(e)}), 500


@bp.route('/api/v1/user-presets', methods=['POST'])
def create_user_preset():
    """
    ENDPOINT 3: POST /api/v1/user-presets (AUTHENTICATED)
    Persists custom configuration string combinations sent from the frontend Generate Card wizard interface.
    """
    payload, err_resp, status = authenticate_request()
    if err_resp:
        return jsonify(err_resp), status
        
    discord_id = payload.get('sub')
    if not discord_id:
        logger.error("[PRESETS ERROR] Token decoded successfully but 'sub' claim is missing on POST.")
        return jsonify({"error": "Unauthorized", "details": "Token missing sub claim"}), 401
        
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Bad Request", "details": "Request body must be a JSON object"}), 400
        
    name = data.get('name')
    description = data.get('description')
    if description is None or not isinstance(description, str):
        description = ''
    flags = data.get('flags')
    
    # Extract creator name from payload or JWT claims
    creator_name = data.get('creator_name') or payload.get('username') or payload.get('name') or 'Discord User'
    
    # Trap Null/Blank values: If 'flags' or 'name' parameters are missing or contain blank strings, reject with 400
        # Trap Null/Blank values and reject duplicate names for this user
    if name is None or not isinstance(name, str) or name.strip() == '':
        return jsonify({"error": "Bad Request", "details": "Parameter 'name' is missing or blank"}), 400
        
    db = get_db()
    
    # Check if this user already has a preset with the same name (case-insensitive)
    existing_query = (
        db.collection('presets')
        .where(filter=FieldFilter('creator_id', '==', discord_id))
        .where(
            filter=FieldFilter(
                'preset_name_lower',
                '==',
                name.strip().lower(),
            )
        )
        .limit(1)
        .stream()
    )
                       
    if list(existing_query):
        return jsonify({
            "error": "Conflict", 
            "details": f"A preset named '{name.strip()}' already exists. Please choose a different name or delete the old one first."
        }), 409
    
    if flags is None or not isinstance(flags, str) or flags.strip() == '':
        return jsonify({"error": "Bad Request", "details": "Parameter 'flags' is missing or blank"}), 400
        
    try:
        doc_ref = get_db().collection('presets').document()
        
        # Format the ISO 8601 UTC timestamp tracking creation time (e.g. "2026-05-21 19:20:00.123456")
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
        
        # Parse standard schema fields with safe defaults
        arguments = str(data.get('arguments') or data.get('preset_arguments') or '')
        gen_count = int(data.get('gen_count') or 0)
        hidden = bool(data.get('hidden') or False)
        validation_error = data.get('validation_error')  # can be None
        validation_status = str(data.get('validation_status') or 'VALID')

        payload_data = {
            'id': doc_ref.id,
            'name': name.strip(),
            'preset_name': name.strip(),
            'preset_name_lower': name.strip().lower(),
            'description': description.strip(),
            'flags': flags.strip(),
            'official': False,  # Strictly hardcoded on backend to prevent user privilege escalation
            'creator_id': discord_id,
            'creator_name': creator_name.strip(),
            'tags': [],
            'created_at': created_at,
            'arguments': arguments,
            'gen_count': gen_count,
            'hidden': hidden,
            'validation_error': validation_error,
            'validation_status': validation_status
        }
        
        doc_ref.set(payload_data)
        return jsonify(payload_data), 201
    except Exception as e:
        logger.error(f"[PRESETS ERROR] Failed to create preset for user {discord_id}: {e}")
        return jsonify({"error": "Internal Server Error", "details": str(e)}), 500


@bp.route('/api/v1/user-presets', methods=['DELETE'])
@bp.route('/api/v1/user-presets/<preset_id>', methods=['DELETE'])
def delete_user_preset(preset_id=None):
    """
    ENDPOINT 4: DELETE /api/v1/user-presets (AUTHENTICATED)
    Deletes a user's preset or lets administrators clean up records.
    Can be specified via path parameter or via 'id' query parameter.
    """
    if not preset_id:
        preset_id = request.args.get('id')
        
    if not preset_id:
        return jsonify({"error": "Bad Request", "details": "Preset 'id' must be provided as a path parameter or a query parameter."}), 400
    payload, err_resp, status = authenticate_request()
    if err_resp:
        return jsonify(err_resp), status
        
    discord_id = payload.get('sub')
    if not discord_id:
        logger.error("[PRESETS ERROR] Token decoded successfully but 'sub' claim is missing on DELETE.")
        return jsonify({"error": "Unauthorized", "details": "Token missing sub claim"}), 401
        
    is_admin = payload.get('isAdmin', False)
    
    try:
        doc_ref = get_db().collection('presets').document(preset_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            return jsonify({"error": "Not Found", "details": f"Preset '{preset_id}' not found"}), 404
            
        preset_data = doc.to_dict()
        creator_id = preset_data.get('creator_id')
        
        # Security Verification Trap: creator_id matches OR user has isAdmin claim flag
        if creator_id == discord_id or is_admin:
            doc_ref.delete()
            return jsonify({"success": True, "message": "Preset deleted successfully"}), 200
        else:
            logger.warning(f"[PRESETS SECURITY WARNING] User {discord_id} attempted unauthorized deletion of preset {preset_id} owned by {creator_id}")
            return jsonify({"error": "Forbidden", "details": "You are not authorized to delete this preset"}), 403
    except Exception as e:
        logger.error(f"[PRESETS ERROR] Failed to delete preset {preset_id}: {e}")
        return jsonify({"error": "Internal Server Error", "details": str(e)}), 500

@bp.route('/api/v1/user-presets', methods=['PUT'])
def update_user_preset():
    """
    ENDPOINT 5: PUT /api/v1/user-presets (AUTHENTICATED)
    Updates an existing preset's properties (tags, flags, description, or download_timestamp).
    Supports:
    - User updating the download_timestamp when generating a seed using a selected preset (flags and presetName in body)
    - Admin or Creator updating the tags, flags or description of a preset (id or name/presetName in body)
    """
    payload, err_resp, status = authenticate_request()
    if err_resp:
        return jsonify(err_resp), status
        
    discord_id = payload.get('sub')
    if not discord_id:
        logger.error("[PRESETS ERROR] Token decoded successfully but 'sub' claim is missing on PUT.")
        return jsonify({"error": "Unauthorized", "details": "Token missing sub claim"}), 401
        
    is_admin = payload.get('isAdmin', False) or payload.get('is_admin', False) or payload.get('isSuperadmin', False)
    
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Bad Request", "details": "Request body must be a JSON object"}), 400
        
    preset_id = data.get('id')
    name = data.get('name') or data.get('presetName')
    flags = data.get('flags')
    tags = data.get('tags')
    description = data.get('description')
    
    db = get_db()
    doc_ref = None
    
    # 1. Locate the document by ID or falls back to name search
    if preset_id:
        if not isinstance(preset_id, str):
            return jsonify({"error": "Bad Request", "details": "Parameter 'id' must be a string"}), 400
        doc_ref = db.collection('presets').document(preset_id)
        doc = doc_ref.get()
        if not doc.exists:
            # Fallback: Check if preset_id is actually a preset name or name string
            presets_ref = db.collection('presets')
            docs = []
            for field in ['preset_name_lower', 'name', 'preset_name']:
                val = preset_id.strip().lower() if field == 'preset_name_lower' else preset_id.strip()
                docs = list(presets_ref.where(field, '==', val).limit(1).stream())
                if docs:
                    break
                
            if not docs:
                return jsonify({"error": "Not Found", "details": f"Preset with id/name '{preset_id}' not found"}), 404
            
            doc_ref = docs[0].reference
            preset_data = docs[0].to_dict()
        else:
            preset_data = doc.to_dict()
    elif name:
        if not isinstance(name, str):
            return jsonify({"error": "Bad Request", "details": "Parameter 'name' or 'presetName' must be a string"}), 400
        if description is not None and not isinstance(description, str):
            return jsonify({"error": "Bad Request", "details": "Parameter 'description' must be a string"}), 400
        if flags is not None and not isinstance(flags, str):
            return jsonify({"error": "Bad Request", "details": "Parameter 'flags' must be a string"}), 400
        if tags is not None and not isinstance(tags, list):
            return jsonify({"error": "Bad Request", "details": "Parameter 'tags' must be a list"}), 400

        presets_ref = db.collection('presets')
        # Search by name. If not admin, restrict search to the user's own presets
        if is_admin:
            query = (
                presets_ref.where(
                    filter=FieldFilter('preset_name_lower', '==', name.strip().lower())
                )
                .limit(1)
                .stream()
            )
        else:
            query = (
                presets_ref.where(
                    filter=FieldFilter('preset_name_lower', '==', name.strip().lower())
                )
                .where(filter=FieldFilter('creator_id', '==', discord_id))
                .limit(1)
                .stream()
            )
        docs = list(query)

        if not docs:
            # Fallback search for public download tracking (updating download_timestamp of official or shared presets)
            query_all = (
                presets_ref.where(
                    filter=FieldFilter('preset_name_lower', '==', name.strip().lower())
                )
                .limit(1)
                .stream()
            )
            docs = list(query_all)
            if not docs:
                if is_admin:
                    doc_ref = db.collection('presets').document()
                    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
                    download_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    is_official = 'official' in tags if tags is not None else False
                    preset_data = {
                        'id': doc_ref.id,
                        'name': name.strip(),
                        'preset_name': name.strip(),
                        'preset_name_lower': name.strip().lower(),
                        'description': description.strip() if description else '',
                        'flags': flags.strip() if flags else '',
                        'official': is_official,
                        'creator_id': 'override',
                        'creator_name': 'override',
                        'tags': tags if tags is not None else [],
                        'created_at': created_at,
                        'download_timestamp': download_timestamp
                    }
                    doc_ref.set(preset_data)
                    return jsonify(preset_data), 200
                else:
                    return jsonify({"error": "Not Found", "details": f"Preset '{name}' not found"}), 404
            else:
                doc_ref = docs[0].reference
                preset_data = docs[0].to_dict()
        else:
            doc_ref = docs[0].reference
            preset_data = docs[0].to_dict()
    else:
        return jsonify({"error": "Bad Request", "details": "Must provide 'id' or 'name'/'presetName' to identify preset"}), 400
        
    creator_id = preset_data.get('creator_id')
    
    # 2. Check permissions: owner can edit, admins can edit, or anyone can track a download
    is_download_update = ('flags' in data or 'presetName' in data) and len(data) <= 3 and 'tags' not in data
    
    if creator_id == discord_id or is_admin or is_download_update:
        update_data = {}
        
        if tags is not None and (is_admin or creator_id == discord_id):
            update_data['tags'] = tags
            if is_admin:
                update_data['official'] = 'official' in tags
                
        if flags is not None and (creator_id == discord_id or is_admin):
            update_data['flags'] = flags
            
        if description is not None and (creator_id == discord_id or is_admin):
            update_data['description'] = description.strip()
            
        # Record/Update the download timestamp
        update_data['download_timestamp'] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        
        doc_ref.update(update_data)
        
        response_data = {**preset_data, **update_data}
        return jsonify(response_data), 200
    else:
        logger.warning(f"[PRESETS SECURITY WARNING] User {discord_id} attempted unauthorized update of preset {preset_id or name}")
        return jsonify({"error": "Forbidden", "details": "You are not authorized to update this preset"}), 403


@bp.route('/api/v1/tags', methods=['GET'])
def get_tags():
    """
    GET /api/v1/tags (PUBLIC)
    Fetches the list of allowed custom preset tags from Firestore.
    """
    try:
        tags_ref = get_db().collection('tags')
        docs = tags_ref.stream()
        tags = sorted([doc.id for doc in docs])
        return jsonify(tags), 200
    except Exception as e:
        logger.error(f"[PRESETS ERROR] Failed to fetch tags: {e}")
        return jsonify({"error": "Internal Server Error", "details": str(e)}), 500


@bp.route('/api/v1/tags', methods=['POST'])
def create_tag():
    """
    POST /api/v1/tags (AUTHENTICATED - ADMIN ONLY)
    Adds a new custom preset tag.
    """
    payload, err_resp, status = authenticate_request()
    if err_resp:
        return jsonify(err_resp), status

    is_admin = payload.get('isAdmin', False) or payload.get('is_admin', False) or payload.get('isSuperadmin', False)
    if not is_admin:
        return jsonify({"error": "Forbidden", "details": "Admin privileges required"}), 403

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or 'tag' not in data:
        return jsonify({"error": "Bad Request", "details": "Missing 'tag' parameter in body"}), 400

    tag_name = str(data['tag']).strip().lower()
    if not tag_name or not all(c.isalnum() or c in '-_' for c in tag_name):
        return jsonify({"error": "Bad Request", "details": "Tag name must be alphanumeric and can only contain hyphens or underscores"}), 400

    try:
        get_db().collection('tags').document(tag_name).set({})
        return jsonify({"success": True, "tag": tag_name}), 201
    except Exception as e:
        logger.error(f"[PRESETS ERROR] Failed to create tag '{tag_name}': {e}")
        return jsonify({"error": "Internal Server Error", "details": str(e)}), 500


@bp.route('/api/v1/tags', methods=['PUT'])
def rename_tag():
    """
    PUT /api/v1/tags (AUTHENTICATED - ADMIN ONLY)
    Renames an existing tag in Firestore and updates all affected presets.
    """
    payload, err_resp, status = authenticate_request()
    if err_resp:
        return jsonify(err_resp), status

    is_admin = payload.get('isAdmin', False) or payload.get('is_admin', False) or payload.get('isSuperadmin', False)
    if not is_admin:
        return jsonify({"error": "Forbidden", "details": "Admin privileges required"}), 403

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or 'oldTag' not in data or 'newTag' not in data:
        return jsonify({"error": "Bad Request", "details": "Missing 'oldTag' or 'newTag' parameter in body"}), 400

    old_tag = str(data['oldTag']).strip().lower()
    new_tag = str(data['newTag']).strip().lower()

    if not old_tag or not new_tag:
        return jsonify({"error": "Bad Request", "details": "Tag names cannot be blank"}), 400

    if not all(c.isalnum() or c in '-_' for c in new_tag):
        return jsonify({"error": "Bad Request", "details": "New tag name must be alphanumeric and can only contain hyphens or underscores"}), 400

    if old_tag == new_tag:
        return jsonify({"success": True}), 200

    try:
        db = get_db()
        batch = db.batch()
        
        # 1. Rename tag document in 'tags' collection
        old_tag_ref = db.collection('tags').document(old_tag)
        new_tag_ref = db.collection('tags').document(new_tag)
        
        if old_tag_ref.get().exists:
            batch.set(new_tag_ref, {})
            batch.delete(old_tag_ref)
        else:
            # If the old tag doc didn't exist for some reason, still create the new one
            batch.set(new_tag_ref, {})

        # 2. Query and update all presets containing the old tag
        presets_ref = db.collection('presets')
        query = (
            presets_ref.where(
                filter=FieldFilter('tags', 'array_contains', old_tag)
            )
            .stream()
        )
        for doc in query:
            preset_data = doc.to_dict()
            current_tags = preset_data.get('tags', [])
            updated_tags = [new_tag if t == old_tag else t for t in current_tags]
            # Maintain uniqueness
            updated_tags = list(dict.fromkeys(updated_tags))
            is_official = 'official' in updated_tags
            
            batch.update(doc.reference, {
                'tags': updated_tags,
                'official': is_official
            })

        batch.commit()
        return jsonify({"success": True}), 200
    except Exception as e:
        logger.error(f"[PRESETS ERROR] Failed to rename tag '{old_tag}' to '{new_tag}': {e}")
        return jsonify({"error": "Internal Server Error", "details": str(e)}), 500


@bp.route('/api/v1/tags', methods=['DELETE'])
def delete_tag():
    """
    DELETE /api/v1/tags (AUTHENTICATED - ADMIN ONLY)
    Deletes a tag from Firestore and removes it from all presets.
    """
    payload, err_resp, status = authenticate_request()
    if err_resp:
        return jsonify(err_resp), status

    is_admin = payload.get('isAdmin', False) or payload.get('is_admin', False) or payload.get('isSuperadmin', False)
    if not is_admin:
        return jsonify({"error": "Forbidden", "details": "Admin privileges required"}), 403

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or 'tag' not in data:
        return jsonify({"error": "Bad Request", "details": "Missing 'tag' parameter in body"}), 400

    tag_to_delete = str(data['tag']).strip().lower()
    if not tag_to_delete:
        return jsonify({"error": "Bad Request", "details": "Tag name cannot be blank"}), 400

    try:
        db = get_db()
        batch = db.batch()
        
        # 1. Delete tag document from 'tags' collection
        batch.delete(db.collection('tags').document(tag_to_delete))

        # 2. Query and update all presets containing the tag
        presets_ref = db.collection('presets')
        query = (
            presets_ref.where(
                filter=FieldFilter('tags', 'array_contains', tag_to_delete)
            )
            .stream()
        )
        for doc in query:
            preset_data = doc.to_dict()
            current_tags = preset_data.get('tags', [])
            updated_tags = [t for t in current_tags if t != tag_to_delete]
            is_official = 'official' in updated_tags
            
            batch.update(doc.reference, {
                'tags': updated_tags,
                'official': is_official
            })

        batch.commit()
        return jsonify({"success": True}), 200
    except Exception as e:
        logger.error(f"[PRESETS ERROR] Failed to delete tag '{tag_to_delete}': {e}")
        return jsonify({"error": "Internal Server Error", "details": str(e)}), 500