import os
import logging
from datetime import datetime, timezone
import jwt
from flask import Blueprint, request, jsonify
from google.cloud import firestore
from google.cloud.firestore import FieldFilter
from api_utils.collections import SEEDLIST

# Set up logging
logger = logging.getLogger(__name__)

bp = Blueprint('seedlist', __name__)

JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY')

_db = None

def get_db():
    global _db
    if _db is None:
        _db = firestore.Client()
    return _db

def authenticate_request_optional():
    """
    Extracts and decodes the bearer token from the Authorization header if present.
    If the header is absent, returns (None, None, 200) signifying an anonymous request.
    If the header is present but invalid/expired, returns (None, error_response_dict, status_code).
    If successful, returns (decoded_payload, None, 200).
    """
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        # User is not logged in, treat as optional/anonymous
        return None, None, 200
    
    if not auth_header.startswith('Bearer '):
        logger.error("[SEEDLIST ERROR] Authorization header is not a Bearer token.")
        return None, {"error": "Unauthorized", "details": "Authorization header must start with Bearer"}, 401
    
    parts = auth_header.split(" ")
    if len(parts) != 2:
        logger.error("[SEEDLIST ERROR] Authorization header has invalid format.")
        return None, {"error": "Unauthorized", "details": "Invalid Authorization header format"}, 401
        
    token = parts[1]
    
    if not JWT_SECRET_KEY:
        logger.error("[SEEDLIST ERROR] JWT_SECRET_KEY is not configured on the server.")
        return None, {"error": "Server error", "details": "JWT_SECRET_KEY is not configured on the server"}, 500
        
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=['HS256'])
        return payload, None, 200
    except jwt.ExpiredSignatureError:
        logger.error("[SEEDLIST ERROR] The authentication token has expired.")
        return None, {"error": "Unauthorized", "details": "Token has expired"}, 401
    except jwt.InvalidTokenError as e:
        logger.error(f"[SEEDLIST ERROR] Invalid token: {e}")
        return None, {"error": "Unauthorized", "details": "Invalid token"}, 401


@bp.route('/seedlist', methods=['POST'])
@bp.route('/api/v1/seedlist', methods=['POST'])
def create_seed_entry():
    """
    Creates a new seed entry in the seedlist Firestore collection.
    Supports authenticated (via JWT) and anonymous users.
    Auto-increments the document ID and writes the new document atomically inside a single transaction.
    """
    # 1. Parse optional auth token
    payload, err_resp, status = authenticate_request_optional()
    if err_resp:
        return jsonify(err_resp), status
        
    # 2. Determine creator id and name
    creator_id = "0"
    creator_name = "anonymous"
    
    if payload:
        discord_id = payload.get('sub')
        if discord_id:
            creator_id = str(discord_id)
        creator_name = payload.get('username') or payload.get('name') or "Discord User"
        
    # 3. Parse and validate request JSON body
    data = request.get_json(silent=True) or {}
    
    seed_type = data.get('seed_type', 'ff6wc')
    if not seed_type or not isinstance(seed_type, str):
        seed_type = 'ff6wc'
        
    random_sprites = data.get('random_sprites')
    if random_sprites is None:
        random_sprites = False
    else:
        random_sprites = bool(random_sprites)
        
    share_url = data.get('share_url')
    server_name = data.get('server_name')
    
    server_id = data.get('server_id')
    if server_id is not None:
        server_id = str(server_id)
            
    channel_name = data.get('channel_name')
    
    channel_id = data.get('channel_id')
    if channel_id is not None:
        channel_id = str(channel_id)
            
    # Always use server-side UTC timestamp for consistency and data integrity
    timestamp_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        
    args_list = data.get('args_list')
    flagstring = data.get('flagstring')
    hash_val = data.get('hash')
    seed_val = data.get('seed')
    
    # 4. Atomic Auto-Increment and Document Creation via a single Firestore Transaction
    db = get_db()
    
    @firestore.transactional
    def create_entry_in_transaction(transaction, counter_ref, db, payload_template):
        snapshot = counter_ref.get(transaction=transaction)
        if snapshot.exists:
            current_id = snapshot.get("last_id")
        else:
            current_id = 0
        new_id = current_id + 1
        
        # Increment counter
        transaction.set(counter_ref, {"last_id": new_id})
        
        # Build and save the document
        doc_ref = db.collection(SEEDLIST).document(str(new_id))
        payload_data = {**payload_template, 'id': new_id}
        transaction.set(doc_ref, payload_data)
        
        return payload_data
        
    try:
        counter_ref = db.collection('counters').document('seedlist')
        transaction = db.transaction()
        saved_payload = create_entry_in_transaction(transaction, counter_ref, db, {
            'creator_id': creator_id,
            'creator_name': creator_name.strip() if isinstance(creator_name, str) else creator_name,
            'seed_type': seed_type.strip(),
            'share_url': share_url.strip() if isinstance(share_url, str) else share_url,
            'server_name': server_name.strip() if isinstance(server_name, str) else server_name,
            'server_id': server_id,
            'channel_name': channel_name.strip() if isinstance(channel_name, str) else channel_name,
            'channel_id': channel_id,
            'random_sprites': random_sprites,
            'timestamp': timestamp_str,
            'args_list': args_list.strip() if isinstance(args_list, str) else args_list,
            'flagstring': flagstring.strip() if isinstance(flagstring, str) else flagstring,
            'hash': hash_val.strip() if isinstance(hash_val, str) else hash_val,
            'seed': seed_val.strip() if isinstance(seed_val, str) else seed_val
        })
        
        return jsonify(saved_payload), 201
    except Exception as e:
        logger.exception(f"[SEEDLIST ERROR] Failed to create seed entry: {e}")
        return jsonify({"error": "Internal Server Error", "details": str(e)}), 500


@bp.route('/seedlist', methods=['GET'])
@bp.route('/api/v1/seedlist', methods=['GET'])
def get_seedlist():
    """
    Fetches the seedlist records from Firestore for reporting.
    Supports filtering by creator_id and seed_type.
    Uses native order_by and limit on the query object for performance and cost efficiency.
    """
    try:
        db = get_db()
        seedlist_ref = db.collection(SEEDLIST)
        query = seedlist_ref
        
        # Filtering by creator_id
        creator_id_param = request.args.get('creator_id')
        if creator_id_param is not None:
            query = query.filter(filter=FieldFilter('creator_id', '==', str(creator_id_param)))
                
        # Filtering by seed_type
        seed_type_param = request.args.get('seed_type')
        if seed_type_param:
            query = query.filter(filter=FieldFilter('seed_type', '==', seed_type_param.strip()))
            
        # Native sort by timestamp descending
        query = query.order_by('timestamp', direction=firestore.Query.DESCENDING)
        
        # Apply limit parameter
        limit_val = 100
        limit_param = request.args.get('limit')
        if limit_param:
            try:
                limit_val = min(int(limit_param), 1000)
            except ValueError:
                return jsonify({"error": "Bad Request", "details": "limit must be an integer"}), 400
                
        query = query.limit(limit_val)
        
        docs = query.stream()
        output = []
        for doc in docs:
            output.append(doc.to_dict())
            
        return jsonify(output), 200
    except Exception as e:
        logger.exception(f"[SEEDLIST ERROR] Failed to fetch seedlist: {e}")
        return jsonify({"error": "Internal Server Error", "details": str(e)}), 500
