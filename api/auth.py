import os
import time
import logging
import secrets
import requests
import jwt
from flask import Blueprint, request, redirect, jsonify

# Set up logging for environment diagnostics
logger = logging.getLogger(__name__)

bp = Blueprint('auth', __name__, url_prefix="/api/v1/auth")

DISCORD_CLIENT_ID = os.environ.get('DISCORD_CLIENT_ID')
DISCORD_CLIENT_SECRET = os.environ.get('DISCORD_CLIENT_SECRET')
DISCORD_REDIRECT_URI = os.environ.get('DISCORD_REDIRECT_URI')
ULTIMA_FRONTEND_URL = os.environ.get('ULTIMA_FRONTEND_URL')
JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY')

DISCORD_API_BASE = "https://discord.com/api/v10"

# Diagnose environment variables on blueprint load
missing_vars = [
    var for var in ['DISCORD_CLIENT_ID', 'DISCORD_CLIENT_SECRET', 'DISCORD_REDIRECT_URI', 'ULTIMA_FRONTEND_URL', 'JWT_SECRET_KEY']
    if not os.environ.get(var)
]
if missing_vars:
    logger.warning(
        f"[AUTH SETUP WARNING] The following environment variables required for Discord OAuth2 are missing: "
        f"{', '.join(missing_vars)}. The authentication flow will fail at runtime until these are configured."
    )


@bp.route('/login', methods=['GET'])
def login():
    # Defensive check to ensure we can build the redirect URL
    if not DISCORD_CLIENT_ID or not DISCORD_REDIRECT_URI:
        logger.error("[AUTH ERROR] Cannot initiate login. DISCORD_CLIENT_ID or DISCORD_REDIRECT_URI is not configured.")
        return jsonify({
            "error": "Server configuration error",
            "details": "Discord client ID or redirect URI is missing."
        }), 500

    # CSRF Protection: Generate a unique, cryptographically strong random state string
    state = secrets.token_urlsafe(32)

    discord_auth_url = (
        f"{DISCORD_API_BASE}/oauth2/authorize"
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={requests.utils.quote(DISCORD_REDIRECT_URI)}"
        f"&response_type=code"
        f"&scope=identify"
        f"&state={state}"
    )
    
    response = redirect(discord_auth_url)
    
    # Secure Cookie Configuration:
    # Disable secure=True only during local testing to support non-HTTPS environments
    is_secure = not DISCORD_REDIRECT_URI.startswith("http://localhost")
    response.set_cookie(
        'oauth_state',
        state,
        max_age=600,  # 10 minutes
        httponly=True,
        secure=is_secure,
        samesite='Lax'
    )
    return response


@bp.route('/callback', methods=['GET'])
def callback():
    # Defensive check to ensure all runtime dependencies are present
    if not all([DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, DISCORD_REDIRECT_URI, ULTIMA_FRONTEND_URL, JWT_SECRET_KEY]):
        logger.error("[AUTH ERROR] Callback invoked but required environment configuration is missing.")
        return jsonify({
            "error": "Server configuration error",
            "details": "One or more required environment variables are not configured."
        }), 500

    # CSRF Protection Validation
    state_cookie = request.cookies.get('oauth_state')
    state_param = request.args.get('state')

    if not state_cookie or not state_param or state_cookie != state_param:
        logger.error(f"[AUTH ERROR] CSRF validation failed. Cookie state: {state_cookie}, Param state: {state_param}")
        return jsonify({
            "error": "Unauthorized",
            "details": "CSRF validation failed: State parameter mismatch or missing."
        }), 403

    code = request.args.get('code')
    if not code:
        return jsonify({"error": "Missing authorization code"}), 400

    token_data = {
        'client_id': DISCORD_CLIENT_ID,
        'client_secret': DISCORD_CLIENT_SECRET,
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': DISCORD_REDIRECT_URI
    }
    token_headers = {'Content-Type': 'application/x-www-form-urlencoded'}

    # 1. Exchange the Authorization Code for an Access Token
    try:
        token_response = requests.post(
            f"{DISCORD_API_BASE}/oauth2/token", 
            data=token_data, 
            headers=token_headers,
            timeout=10
        )
    except requests.exceptions.RequestException as e:
        logger.error(f"[AUTH ERROR] HTTP request to Discord token endpoint failed: {e}")
        return jsonify({"error": "Failed to contact Discord token exchange service"}), 502

    if token_response.status_code != 200:
        logger.error(f"[AUTH ERROR] Discord token exchange failed: {token_response.text}")
        return jsonify({"error": "Failed to exchange token with Discord", "details": token_response.text}), 400
        
    access_token = token_response.json().get('access_token')
    if not access_token:
        logger.error("[AUTH ERROR] Discord token response did not contain access_token.")
        return jsonify({"error": "Failed to retrieve access token from Discord"}), 400

    # 2. Fetch User Profile Details from Discord
    user_headers = {'Authorization': f"Bearer {access_token}"}
    try:
        user_response = requests.get(
            f"{DISCORD_API_BASE}/users/@me", 
            headers=user_headers,
            timeout=10
        )
    except requests.exceptions.RequestException as e:
        logger.error(f"[AUTH ERROR] HTTP request to Discord profile endpoint failed: {e}")
        return jsonify({"error": "Failed to contact Discord user profile service"}), 502

    if user_response.status_code != 200:
        logger.error(f"[AUTH ERROR] Discord profile request failed: {user_response.text}")
        return jsonify({"error": "Failed to fetch user profile from Discord"}), 400
        
    discord_user = user_response.json()

    # SECURITY DEFENSE: Null Value Trap
    # Always verify that Discord returned a valid user ID string before signing the JWT.
    if not discord_user or not discord_user.get('id'):
        logger.error(f"[AUTH ERROR] Discord returned an invalid or empty user profile: {discord_user}")
        return jsonify({"error": "Invalid user profile received from Discord"}), 400

    # 3. Construct JWT Session Token for Ultima
    # Set expiration to 7 days in the future
    payload = {
        'sub': str(discord_user['id']),
        'username': discord_user.get('username'),
        'avatar': discord_user.get('avatar'),
        'exp': int(time.time()) + (7 * 24 * 60 * 60)
    }

    try:
        session_token = jwt.encode(payload, JWT_SECRET_KEY, algorithm='HS256')
    except Exception as e:
        logger.error(f"[AUTH ERROR] JWT signing failed: {e}")
        return jsonify({"error": "Failed to generate session token"}), 500

    # 4. Redirect the Client back to Ultima Frontend using a URL fragment (#token=)
    # This prevents the JWT from leaking in server logs or Referer headers.
    target_url = f"{ULTIMA_FRONTEND_URL}/login-success#token={session_token}"
    response = redirect(target_url)
    
    # Clean up the state cookie after a successful login flow completes
    response.delete_cookie('oauth_state')
    return response
