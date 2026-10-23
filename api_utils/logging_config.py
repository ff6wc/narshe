import json
import logging
import os
import re
import sys
import uuid

from flask import g, has_request_context, request

# Fields passed with logger.x(..., extra={...}) that end up in the JSON entry
EXTRA_FIELDS = ("flags", "return_code", "duration_s", "stdout", "stderr", "error_summary", "wc_commit", "source")

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class CloudRunJsonFormatter(logging.Formatter):
  """One JSON object per line. Cloud Run turns it into a structured log entry:
  'severity' sets the level and the other keys become searchable jsonPayload
  fields, so a multi-line traceback stays in one entry instead of one per line.
  """
  def format(self, record):
    entry = {
      "severity": record.levelname,
      "message": record.getMessage(),
      "logger": record.name,
    }
    if has_request_context():
      entry["request_id"] = getattr(g, "request_id", None)
      entry["path"] = request.path
    revision = os.getenv("K_REVISION")
    if revision:
      entry["revision"] = revision
    for field in EXTRA_FIELDS:
      if hasattr(record, field):
        entry[field] = getattr(record, field)
    if record.exc_info:
      entry["exception"] = self.formatException(record.exc_info)
    return json.dumps(entry, default=str)


def configure_logging():
  """Send all app logging to stdout as JSON at INFO and above. Without this the
  root logger has no handler, so logger.info() calls are dropped and errors are
  written as bare lines with no severity.
  """
  handler = logging.StreamHandler(sys.stdout)
  handler.setFormatter(CloudRunJsonFormatter())
  root = logging.getLogger()
  root.handlers = [handler]
  root.setLevel(logging.INFO)


def get_request_id():
  """Return the caller's X-Request-ID if it looks like an ID, else a new one,
  so seedbot/ultima reports and these logs can be matched up.
  """
  request_id = request.headers.get("X-Request-ID", "")
  if _REQUEST_ID_PATTERN.match(request_id):
    return request_id
  return uuid.uuid4().hex[:12]


def get_wc_commit():
  """Return the WorldsCollide commit baked into the image (see Dockerfile), or 'unknown'."""
  try:
    with open(os.path.join(os.path.dirname(__file__), "..", "WorldsCollide", ".wc_commit")) as commit_file:
      return commit_file.read().strip() or "unknown"
  except OSError:
    return "unknown"
