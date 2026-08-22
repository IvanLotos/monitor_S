#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import yaml
import json
import logging
import requests
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.auth import HTTPBasicAuth
from urllib.parse import urlparse

# Подавление предупреждений о небезопасных HTTPS-запросах (verify=False)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

VERSION = "1.0.0"
logger = logging.getLogger(__name__)


def resolve_env(value):
    if isinstance(value, str) and value.startswith('$'):
        env_var = value[1:]
        return os.getenv(env_var, value)
    return value


def normalize_path(path):
    if not path:
        path = '/'
    path = path.split('?')[0]
    if len(path) > 1 and path.endswith('/'):
        path = path.rstrip('/')
    escaped = path.replace('/', '_')
    cleaned = re.sub(r'[^a-zA-Z0-9_.-]', '', escaped)
    if not cleaned:
        cleaned = '_'
    return cleaned


def mask_secrets(text, secrets):
    if not secrets:
        return text
    masked = text
    for secret in secrets:
        if secret:
            masked = masked.replace(secret, '***')
    return masked


def load_secrets(secrets_file):
    if not secrets_file:
        return {}
    if not os.path.isfile(secrets_file):
        logger.error(f"Secrets file not found: {secrets_file}")
        return {}
    try:
        with open(secrets_file, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
        secrets = {}
        for ref, creds in data.items():
            user = creds.get('user', '')
            password = creds.get('password', '')
            if user and password:
                secrets[ref] = (user, password)
            else:
                logger.warning(f"Secret ref '{ref}' missing user or password, skipped")
        return secrets
    except Exception as e:
        logger.error(f"Failed to load secrets file {secrets_file}: {e}")
        return {}


class HTTPChecker:
    def __init__(self, config):
        self.config = self._resolve_env_in_config(config)
        self.prefix = self.config.get('prefix', 'http')
        self.secrets_file = self.config.get('secrets_file')
        self.log_level = self.config.get('log_level', 'summary')
        self.defaults = self.config.get('defaults', {})
        self.endpoints = self.config.get('endpoints', [])
        self.max_workers = self.config.get('max_workers', 20)
        self.secrets = load_secrets(self.secrets_file)
        if self.secrets_file and not self.secrets:
            logger.warning("Secrets file specified but no secrets loaded")
        logger.info(f"HTTP module initialized with {len(self.endpoints)} endpoints, max_workers={self.max_workers}")

    def _resolve_env_in_config(self, obj):
        if isinstance(obj, dict):
            return {k: self._resolve_env_in_config(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._resolve_env_in_config(item) for item in obj]
        else:
            return resolve_env(obj)

    def _get_auth(self, endpoint):
        auth_ref = endpoint.get('auth_ref')
        auth_user = endpoint.get('auth_user')
        auth_password = endpoint.get('auth_password')
        secrets = []
        if auth_ref:
            if auth_ref in self.secrets:
                user, password = self.secrets[auth_ref]
                secrets.append(password)
                return HTTPBasicAuth(user, password), secrets
            else:
                logger.error(f"auth_ref '{auth_ref}' not found in secrets")
                return None, []
        if auth_user and auth_password:
            secrets.append(auth_password)
            return HTTPBasicAuth(auth_user, auth_password), secrets
        headers = endpoint.get('headers', {})
        auth_header = headers.get('Authorization')
        if auth_header and auth_header.startswith('Basic'):
            secrets.append(auth_header)
        return None, secrets

    def _log_request(self, endpoint, method, url, status, elapsed, response_text='',
                     error=None, log_level_override=None):
        level = log_level_override or endpoint.get('log_level', self.log_level)
        if level == 'errors' and error is None:
            return
        log_parts = [f"{method} {url} -> {status if status else 'ERROR'} ({elapsed:.3f}s)"]
        if error:
            log_parts.append(f"ERROR: {error}")
        if level == 'full':
            headers = endpoint.get('headers', {})
            _, secrets = self._get_auth(endpoint)
            headers_str = ', '.join([f"{k}: {mask_secrets(v, secrets)}" for k, v in headers.items()])
            log_parts.append(f"Headers: {headers_str}")
            if response_text:
                body_preview = response_text[:1000] + ('...' if len(response_text) > 1000 else '')
                log_parts.append(f"Body: {body_preview}")
        logger.info(' | '.join(log_parts))

    def _check_single(self, endpoint):
        host = endpoint.get('host')
        port = endpoint.get('port')
        path = endpoint.get('path', '/')
        ssl = endpoint.get('ssl', self.defaults.get('ssl', 'auto'))
        method = endpoint.get('method', self.defaults.get('method', 'GET')).upper()
        expected_status = endpoint.get('expected_status', self.defaults.get('expected_status', 200))
        check_content = endpoint.get('check_content', self.defaults.get('check_content'))
        timeout = endpoint.get('timeout', self.defaults.get('timeout', 3))
        headers = endpoint.get('headers', {}).copy()
        body = endpoint.get('body')
        log_level_override = endpoint.get('log_level')

        if ssl == 'auto':
            ssl_enabled = (port == 443)
        else:
            ssl_enabled = bool(ssl)
        scheme = 'https' if ssl_enabled else 'http'
        url = f"{scheme}://{host}:{port}{path}"

        auth, secrets = self._get_auth(endpoint)
        if auth is None and endpoint.get('auth_ref'):
            key = f"{self.prefix}.{host.split('.')[0] if '.' in host else host}.{port}.{method}.{normalize_path(path)}"
            return key, False

        request_data = None
        if method == 'POST' and body:
            if isinstance(body, dict):
                request_data = json.dumps(body)
            else:
                request_data = body
            if 'Content-Type' not in headers:
                headers['Content-Type'] = 'application/json'

        try:
            response = requests.request(
                method=method,
                url=url,
                headers=headers,
                auth=auth,
                timeout=timeout,
                data=request_data,
                verify=False,
            )
            status_ok = response.status_code in (expected_status if isinstance(expected_status, list) else [expected_status])
            content_ok = True
            if check_content and status_ok:
                if check_content.startswith('re:'):
                    pattern = check_content[3:]
                    content_ok = bool(re.search(pattern, response.text, re.IGNORECASE))
                else:
                    content_ok = check_content in response.text
            success = status_ok and content_ok
            self._log_request(
                endpoint=endpoint,
                method=method,
                url=url,
                status=response.status_code,
                elapsed=response.elapsed.total_seconds(),
                response_text=response.text if log_level_override == 'full' else '',
                log_level_override=log_level_override
            )
        except requests.exceptions.Timeout:
            self._log_request(endpoint, method, url, None, 0, error='Timeout', log_level_override=log_level_override)
            success = False
        except requests.exceptions.ConnectionError as e:
            self._log_request(endpoint, method, url, None, 0, error=f'ConnectionError: {e}', log_level_override=log_level_override)
            success = False
        except Exception as e:
            self._log_request(endpoint, method, url, None, 0, error=f'Exception: {e}', log_level_override=log_level_override)
            success = False

        first_label = host.split('.')[0] if '.' in host else host
        escaped_path = normalize_path(path)
        key = f"{self.prefix}.{first_label}.{port}.{method}.{escaped_path}"
        return key, success

    def run(self):
        results = {}
        tasks = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            for endpoint in self.endpoints:
                tasks.append(executor.submit(self._check_single, endpoint))
            for future in as_completed(tasks):
                try:
                    key, success = future.result()
                    results[key] = success
                except Exception as e:
                    logger.error(f"Unexpected error in http_check: {e}")
        return results


def run(config):
    checker = HTTPChecker(config)
    return checker.run()