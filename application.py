import re
from dotenv import load_dotenv

# read in the env variables first, before importing local blueprints that load env at module-level
load_dotenv('.env')

from flask import Flask, make_response
from flask_cors import CORS
import requests
import api.metadata
import api.generate
import api.log
import api.portrait
import api.seed
import api.sprite
import api.sprites
import api.wc
import api.auth
import api.presets

application = Flask(__name__)
CORS(application, origins=[
    "https://ff6worldscollide.com", "https://dev.ff6worldscollide.com",
    re.compile(r"^https://.*ff6worldscollide\.pages\.dev$"),
    "http://localhost:3000"
])

@application.route("/", methods=["GET"])
def hello_world():
    import os
    return f"<p>{os.getenv('HELLO_TEXT')}</p>"


@application.route("/sotws", methods=["GET"])
def get_sotws():
    resp = requests.get("https://storage.googleapis.com/seedbot/sotw_db.json", stream=True)
    return (resp.raw.read(), resp.status_code, resp.headers.items()) 

#register the endpoints
application.register_blueprint(api.auth.bp)
application.register_blueprint(api.presets.bp)

application.register_blueprint(api.generate.bp)

application.register_blueprint(api.log.bp)

application.register_blueprint(api.metadata.bp)

@application.route("/api/music/generate", methods=["POST"])
def post_generate_music():
    return make_response("Unsupported", 500)

application.register_blueprint(api.portrait.bp)

application.register_blueprint(api.seed.bp)

application.register_blueprint(api.sprite.bp)

application.register_blueprint(api.sprites.bp)

application.register_blueprint(api.wc.bp)

