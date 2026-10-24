from flask import Blueprint, Response
import sys

bp = Blueprint('wc', __name__, url_prefix="/api")

@bp.route("/wc", methods=["GET"])
def get_manifest_web():
    sys.path.append("WorldsCollide")
    
    from WorldsCollide.api.get_manifest import get_manifest
    manifest = get_manifest()
    # Which build is answering: the WorldsCollide commit cloned into the image
    # (the version string doesn't change between dev commits) and the Cloud Run revision
    import os
    from api_utils.logging_config import get_wc_commit
    manifest['wc_commit'] = get_wc_commit()
    manifest['revision'] = os.getenv('K_REVISION', 'local')

    import json
    return Response(
        response = json.dumps(manifest).encode(),
        status = 200,
        mimetype='application/json'
    )