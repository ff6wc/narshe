from api_utils.get_seed_payload import get_seed_payload

from api_utils.get_seed_url import get_seed_url
from api_utils.create_seed import create_seed
from api_utils.logging_config import get_wc_commit

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import logging
from flask import Response, g

logger = logging.getLogger(__name__)

class GenerateHandler():
  def __init__(self, include_patch, include_log, use_protocol):
    self.include_patch = include_patch
    self.include_log = include_log
    self.use_protocol = use_protocol # to do: make sure it's recaptcha or api_key

  def get_created_by(self, post_data):
    protocol = self.use_protocol
    if protocol == 'recaptcha':
      return "Website"
    else: 
      from api_utils.get_api_key import get_api_key
      raw_key = post_data['key']
      api_key = get_api_key(raw_key)
      return api_key['name']

  # Return (200, None) if valid, (string, status) if error
  def validate_recaptcha(self, post_data):
    recaptcha_token = post_data['reCAPTCHA']
    secret = os.getenv("RECAPTCHA_SECRET")
    from urllib import request, parse
    data = parse.urlencode({
      "secret": secret,
      "response": recaptcha_token
    }).encode()
    req =  request.Request('https://www.google.com/recaptcha/api/siteverify', data=data)
    resp = request.urlopen(req)

    raw_response = resp.read()
    result = json.loads(raw_response)
    if not result['success']:
      return (403, f'Google recaptcha error! Result: {result}')

    return (200, None)
  
  # Return (200, None) if valid, (string, status) if error
  def validate_api_key(self, post_data):
      from api_utils.get_api_key import get_api_key
      raw_key = post_data['key']
      api_key = get_api_key(raw_key)
      if api_key is None:
        return (400, 'Api key is invalid')
      
      return (200, None)
      
  def do_POST(self, request):
    # 1. Early Input Validation & Parsing (Strict Client Payload check)
    try:
      post_data = request.data
      data = json.loads(post_data)
      original_flags = data['flags']
    except (json.JSONDecodeError, KeyError, TypeError) as e:
      try:
        bad_payload = request.get_data(as_text=True)
        try:
          payload_data = json.loads(bad_payload)
          if isinstance(payload_data, dict):
            for sensitive_key in ["key", "reCAPTCHA"]:
              if sensitive_key in payload_data:
                payload_data[sensitive_key] = "********"
            logger.error(f"Inbound payload validation failure: {json.dumps(payload_data)}")
          else:
            logger.error("Inbound payload is not a JSON object")
        except Exception:
          import re
          sanitized_payload = re.sub(r'("key"\s*:\s*")[^"]+(")', r'\1********\2', bad_payload)
          sanitized_payload = re.sub(r'("reCAPTCHA"\s*:\s*")[^"]+(")', r'\1********\2', sanitized_payload)
          logger.error(f"Inbound payload is not valid JSON: {sanitized_payload}")
      except Exception as log_err:
        logger.error(f"Failed to extract payload for logging: {log_err}")
        
      logger.exception("Seed request validation failed (invalid payload JSON or missing 'flags').")
      return Response (
        response = json.dumps({
          'errors': ['Seed generation failed due to invalid request payload.'],
          'success': False
        }).encode(),
        status = 400,
        mimetype='application/json',
      )

    # 2. Main Generation Execution Pipeline
    try:
      if "WorldsCollide" not in sys.path:
        sys.path.append("WorldsCollide")
      with tempfile.TemporaryDirectory() as dir:
        in_filename = dir + "/ff3.smc"
        from api_utils.generate_seed import generate_seed
        seed_id = generate_seed()
        base_filename = f"ff6wc_{seed_id}"
        out_filename = dir + f"/{base_filename}.smc"
        log_filename = dir + f"/{base_filename}.txt"
        manifest_filename = dir + f"/{base_filename}.json"
        website_url = get_seed_url(seed_id)

        protocol =  self.use_protocol
        logger.info(f'using {protocol} validation protocol')
        (status, error) = self.validate_api_key(data) if protocol == 'api_key' else self.validate_recaptcha(data)
        logger.info(f"{protocol} returned with status {status}")
        if protocol == 'api_key' and status == 403:
          return Response(
            response = json.dumps({
              'errors': ['Invalid api key'],
              'success': False
            }).encode(),
            status = 403,
            mimetype='application/json'
          )
        elif status != 200:
          return Response(
            response = json.dumps({
              'errors': [f'Validation returned with status code {status}: {error}'],
              'success': False
            }).encode(),
            status = 500,
            mimetype='application/json'
          )

        description = data.get('description')
        flags = original_flags +  f' -url {website_url} -manifest {manifest_filename}'
        
        started = time.monotonic()
        result_code, stdout_str, stderr_str = self._run_worlds_collide(in_filename, out_filename, manifest_filename, flags)
        duration_s = round(time.monotonic() - started, 2)

        if result_code != 0:
          error_summary = self._error_summary(stderr_str)
          # One entry with everything needed to triage, searchable by request_id
          logger.error(f"WorldsCollide failed: {error_summary or 'exit code ' + str(result_code)}", extra={
            'flags': original_flags,
            'return_code': result_code,
            'duration_s': duration_s,
            'stdout': stdout_str[-2000:],
            'stderr': stderr_str,
            'error_summary': error_summary,
            'wc_commit': get_wc_commit(),
            'source': protocol,
          })
          return Response (
            response = json.dumps({
              'errors': ['Seed generation failed. See server logs for details.'],
              'success': False,
              'stderr': self._sanitize_stderr(stderr_str),
              'flags': original_flags,
              'request_id': g.get('request_id'),
            }).encode(),
            status = 400,
            mimetype='application/json',
          )
        else:
          logger.info("Seed generated", extra={
            'flags': original_flags,
            'duration_s': duration_s,
            'wc_commit': get_wc_commit(),
            'source': protocol,
          })
          wc_filename = out_filename
          #if os.getenv("NEXT_PUBLIC_ENABLE_BETA") == "true":
          #  wc_filename = dir + f"/{base_filename}-beta.smc"
          #  logger.debug(out_filename, wc_filename)
          #  self._apply_beta_changes(out_filename, wc_filename)
          patch_filename = dir + "/patch.xdelta3"
          try:
            # Run native xdelta3 CLI to generate the patch, disabling secondary compression
            # to ensure compatibility with JavaScript-based web decoders.
            subprocess.run(["xdelta3", "-e", "-S", "none", "-s", in_filename, wc_filename, patch_filename], check=True)
          except subprocess.CalledProcessError as e:
            logger.error(f"xdelta3 command failed with exit code {e.returncode}")
            return Response (
              response = json.dumps({
                'errors': ['Delta patch generation failed. See server logs for details.'],
                'success': False
              }).encode(),
              status = 500,
              mimetype='application/json',
            )

          with open(patch_filename, "rb") as patchfile, open(log_filename, "rb") as logfile, open(manifest_filename, "rb") as manifestfile:
            raw_patch = patchfile.read()

            log_bytes = logfile.read()
            log = log_bytes.decode('utf-8')
            
            import base64
            manifest = json.loads(manifestfile.read())
            patch = base64.b64encode(raw_patch).decode('utf-8')
            
            include_log = self.include_log
            include_patch = self.include_patch

            created_by = self.get_created_by(data)

            raw_seed = create_seed(
              seed_id = seed_id, 
              patch = patch, 
              log = log, 
              website_url = website_url, 
              filename = base_filename, 
              flags = manifest['flags'], 
              seed_type = "ff6wc", 
              description = description,
              version = manifest['version'],
              hash = manifest['hash'],
              created_by = created_by
            )
            
            seed = get_seed_payload(
              raw_seed, 
              log if include_log else None, 
              patch if include_patch else None,
              website_url=get_seed_url(seed_id),
              filename=base_filename
            )

            return Response (
              response = json.dumps(seed).encode(),
              status = 200,
              mimetype='application/json',
            )
    except Exception as e:
      try:
        bad_payload = request.get_data(as_text=True)
        try:
          payload_data = json.loads(bad_payload)
          if isinstance(payload_data, dict):
            for sensitive_key in ["key", "reCAPTCHA"]:
              if sensitive_key in payload_data:
                payload_data[sensitive_key] = "********"
            logger.error(f"Generation failure inbound payload: {json.dumps(payload_data)}")
          else:
            logger.error("Generation failure inbound payload is not a JSON object")
        except Exception:
          import re
          sanitized_payload = re.sub(r'("key"\s*:\s*")[^"]+(")', r'\1********\2', bad_payload)
          sanitized_payload = re.sub(r'("reCAPTCHA"\s*:\s*")[^"]+(")', r'\1********\2', sanitized_payload)
          logger.error(f"Generation failure inbound payload is not valid JSON: {sanitized_payload}")
      except Exception as log_err:
        logger.error(f"Failed to extract payload for logging: {log_err}")
        
      logger.exception("Seed generation pipeline encountered an unhandled exception.")

      return Response (
        response = json.dumps({
          'errors': ['Seed generation failed. See server logs for details.'],
          'success': False,
          'request_id': g.get('request_id'),
        }).encode(),
        status = 500,
        mimetype='application/json',
      )

  def _apply_beta_changes(self, wc_filename, new_filename):
    cwd = os.getcwd()  + "/WorldsCollideConfig"

    executable = cwd + "/wc_config.py"

    red_window_arg = '252828.202222.161616.101010.050606.313131.140606'

    args = ['python', executable, '-i', wc_filename, '-o', new_filename, "-bs", '6', "-ms", "1", '-w1', red_window_arg]
    logger.debug(f'running command {args}')

    return subprocess.Popen(args, cwd = cwd).wait()

  def _run_worlds_collide(self, in_filename, out_filename, manifest_filename, flags):
    src_file = os.getenv("FF3_INPUT_ROM") or 'ff3.smc'
    
    if src_file.startswith('http'):
      urllib.request.urlretrieve(src_file, in_filename)
    else:
      shutil.copyfile(src_file, in_filename)

    cwd = os.getcwd()  + "/WorldsCollide"

    executable = cwd + "/wc.py"

    args = ['python', executable, '-i', in_filename, '-o', out_filename, '-manifest', manifest_filename] + flags.split()
    logger.info(f'running command {args}')

    proc = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = proc.communicate()

    # failures are logged by do_POST as one structured entry
    return proc.returncode, stdout.decode('utf-8', errors='replace'), stderr.decode('utf-8', errors='replace')

  def _error_summary(self, stderr):
    # The last line of a Python traceback names the exception, e.g.
    # "TypeError: 'NoneType' object cannot be interpreted as an integer"
    if not stderr or "Traceback (most recent call last):" not in stderr:
      return None
    lines = [line.strip() for line in stderr.strip().splitlines() if line.strip()]
    return lines[-1][:300] if lines else None

  def _sanitize_stderr(self, stderr):
    if not stderr:
      return ""

    sanitized_lines = []
    for line in stderr.splitlines():
      line_lower = line.lower()
      # Expose only explicit argparse errors or clean usage messages
      if "wc.py: error:" in line or "unrecognized arguments:" in line_lower:
        sanitized_lines.append(line.strip())
      elif "argument" in line_lower and ("invalid" in line_lower or "expected" in line_lower):
        sanitized_lines.append(line.strip())
      elif "usage: wc.py" in line_lower:
        sanitized_lines.append(line.strip())
    
    if sanitized_lines:
      return "\n".join(sanitized_lines)

    # Not a flag error: show which exception the randomizer hit (no traceback)
    error_summary = self._error_summary(stderr)
    if error_summary:
      return f"Randomizer error: {error_summary}"

    return "Seed generation failed due to an error in flag processing."
