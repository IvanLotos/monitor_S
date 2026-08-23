#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import re
import json
import yaml
import importlib.util
import sys
import logging
import logging.config
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import threading
import time
import copy
from datetime import datetime
import socket
import concurrent.futures
import socketserver
import hashlib
 
import requests
from requests.auth import HTTPBasicAuth
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
 
VERSION = "2.1.0"
 
MODULES = {}
GLOBAL_CONFIG = {}
CACHE = {}
CACHE_LOCK = threading.RLock()
CONFIG_LOCK = threading.RLock()
CACHE_TIMESTAMP = None
HOSTNAME = socket.gethostname()
STOP_BACKGROUND = False
 
# ----------------------------------------------------------------------
# Настройка логирования
# ----------------------------------------------------------------------
def setup_logging(config: dict) -> None:
    log_cfg = config.get('logging', {})
    level = getattr(logging, log_cfg.get('level', 'INFO').upper())
    log_format = log_cfg.get('format', '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    datefmt = log_cfg.get('datefmt', '%Y-%m-%d %H:%M:%S')
 
    handlers = []
    if 'file' in log_cfg:
        from logging.handlers import RotatingFileHandler
        log_file = log_cfg['file']
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=log_cfg.get('max_bytes', 10485760),
            backupCount=log_cfg.get('backup_count', 5)
        )
        file_handler.setFormatter(logging.Formatter(log_format, datefmt))
        handlers.append(file_handler)
    else:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter(log_format, datefmt))
        handlers.append(console_handler)
 
    logging.basicConfig(level=level, handlers=handlers)
 
    for mod, lvl in log_cfg.get('module_levels', {}).items():
        logging.getLogger(mod).setLevel(getattr(logging, lvl.upper()))
 
    logging.info("Logging configured")
 
# ----------------------------------------------------------------------
# Вспомогательные функции для работы с файлами
# ----------------------------------------------------------------------
def get_file_hash(filepath: Path) -> str:
    hasher = hashlib.sha1()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b''):
            hasher.update(chunk)
    return hasher.hexdigest()
 
def get_config_files(modules_dir: str) -> list:
    files = [Path('config.yaml')]
    modules_path = Path(modules_dir)
    if modules_path.exists():
        for yaml_file in modules_path.glob('*.yaml'):
            files.append(yaml_file)
    return files
 
def load_yaml_file(filepath: Path) -> dict:
    with open(filepath, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}
 
# ----------------------------------------------------------------------
# Загрузка модулей
# ----------------------------------------------------------------------
def load_modules(modules_dir: str, global_config: dict) -> dict:
    modules = {}
    modules_path = Path(modules_dir)
    if not modules_path.exists():
        logging.warning(f"Modules directory '{modules_dir}' does not exist, creating")
        modules_path.mkdir(parents=True)
 
    for py_file in modules_path.glob('*.py'):
        if py_file.stem.startswith('_'):
            continue
        try:
            spec = importlib.util.spec_from_file_location(py_file.stem, py_file)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if not hasattr(module, 'run'):
                logging.warning(f"Module {py_file.stem} has no 'run' function, skipping")
                continue
 
            version = getattr(module, 'VERSION', 'unknown')
            config_file = py_file.with_suffix('.yaml')
            if not config_file.exists():
                config_file = py_file.with_suffix('.json')
            config = {}
            if config_file.exists():
                with open(config_file, 'r', encoding='utf-8') as f:
                    if config_file.suffix == '.yaml':
                        config = yaml.safe_load(f) or {}
                    else:
                        config = json.load(f) or {}
            else:
                logging.warning(f"Config file for module {py_file.stem} not found, using empty config")
 
            module_name = py_file.stem.replace('_', '')
            modules[module_name] = {
                'run': module.run,
                'config': config,
                'version': version,
                'config_path': str(config_file) if config_file.exists() else None,
                'original_name': py_file.stem
            }
            logging.info(f"Loaded module: {module_name} (from {py_file.stem}, version {version})")
        except Exception as e:
            logging.error(f"Failed to load module {py_file.stem}: {e}", exc_info=True)
 
    return modules
 
# ----------------------------------------------------------------------
# Выполнение модулей с таймаутом
# ----------------------------------------------------------------------
def run_module_with_timeout(module_func, config, timeout):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(module_func, config)
        try:
            result = future.result(timeout=timeout)
            return result if isinstance(result, dict) else {}
        except concurrent.futures.TimeoutError:
            logging.error(f"Module execution timed out after {timeout}s")
            return {}
        except Exception as e:
            logging.error(f"Module execution failed: {e}")
            return {}
 
def collect_results_from_modules(module_names=None):
    timeout = GLOBAL_CONFIG.get('module_timeout', 30)
    merged = {}
    if module_names is None:
        modules_to_run = MODULES.items()
    else:
        modules_to_run = [(name, MODULES[name]) for name in module_names if name in MODULES]
 
    for module_name, module_info in modules_to_run:
        try:
            result = run_module_with_timeout(module_info['run'], module_info['config'], timeout)
            if not isinstance(result, dict):
                logging.warning(f"Module {module_name} returned non-dict, skipped")
                continue
            for k, v in result.items():
                if k in merged:
                    logging.warning(f"Key '{k}' already exists, overwriting from {module_name}")
                merged[k] = v
            logging.info(f"Module {module_name} returned {len(result)} keys")
        except Exception as e:
            logging.error(f"Module {module_name} failed: {e}", exc_info=True)
    return merged
 
# ----------------------------------------------------------------------
# Управление кэшем
# ----------------------------------------------------------------------
def update_cache():
    global CACHE, CACHE_TIMESTAMP
    logging.info("Starting cache update")
    module_names = [name for name in MODULES.keys() if name != 'gettime']
    merged = collect_results_from_modules(module_names)
 
    if 'gettime' in MODULES:
        try:
            gettime_result = run_module_with_timeout(
                MODULES['gettime']['run'],
                MODULES['gettime']['config'],
                GLOBAL_CONFIG.get('module_timeout', 30)
            )
            for k, v in gettime_result.items():
                if k in merged:
                    logging.warning(f"Key '{k}' already exists, overwriting with gettime")
                merged[k] = v
        except Exception as e:
            logging.error(f"Failed to run gettime: {e}")
 
    new_cache = {'timestamp': datetime.now().isoformat()}
    for key, value in merged.items():
        parts = key.split('.')
        if len(parts) == 1:
            new_cache[parts[0]] = value
        elif len(parts) == 2:
            op, host = parts
            new_cache.setdefault(op, {})[host] = value
        else:
            op = parts[0]
            host = '.'.join(parts[1:-1])
            port = parts[-1]
            new_cache.setdefault(op, {}).setdefault(host, {})[port] = value
 
    with CACHE_LOCK:
        CACHE = new_cache
        CACHE_TIMESTAMP = new_cache['timestamp']
    logging.info(f"Cache updated successfully at {CACHE_TIMESTAMP}")
 
# ----------------------------------------------------------------------
# Перезагрузка конфигурации (без рестарта)
# ----------------------------------------------------------------------
def reload_configuration() -> bool:
    global GLOBAL_CONFIG, MODULES
    new_config = None
    new_modules_configs = {}
    try:
        config_path = Path('config.yaml')
        if not config_path.exists():
            logging.error("config.yaml not found, cannot reload")
            return False
        new_config = load_yaml_file(config_path)
 
        old_host = GLOBAL_CONFIG.get('host')
        new_host = new_config.get('host')
        old_port = GLOBAL_CONFIG.get('port')
        new_port = new_config.get('port')
        old_max_workers = GLOBAL_CONFIG.get('server', {}).get('max_workers')
        new_max_workers = new_config.get('server', {}).get('max_workers')
        old_refresh = GLOBAL_CONFIG.get('cache', {}).get('refresh_interval')
        new_refresh = new_config.get('cache', {}).get('refresh_interval')
        old_modules_dir = GLOBAL_CONFIG.get('modules_dir')
        new_modules_dir = new_config.get('modules_dir')
        old_watch_interval = GLOBAL_CONFIG.get('config_watch_interval')
        new_watch_interval = new_config.get('config_watch_interval')
 
        if (old_host != new_host or old_port != new_port or
            old_max_workers != new_max_workers or old_refresh != new_refresh or
            old_modules_dir != new_modules_dir or
            old_watch_interval != new_watch_interval):
            logging.error(
                "Forbidden configuration change detected: host, port, server.max_workers, "
                "cache.refresh_interval, modules_dir, or config_watch_interval changed. "
                "Manual restart required. Changes were NOT applied."
            )
            return False
 
        modules_dir = new_config.get('modules_dir', GLOBAL_CONFIG.get('modules_dir', './modules'))
        for module_name, module_info in MODULES.items():
            orig_name = module_info['original_name']
            config_file = Path(modules_dir) / (orig_name + '.yaml')
            if config_file.exists():
                try:
                    new_mod_config = load_yaml_file(config_file)
                    new_modules_configs[module_name] = new_mod_config
                except Exception as e:
                    logging.error(f"Failed to load module config for {module_name}: {e}")
                    return False
            else:
                logging.warning(f"Config file for module {module_name} not found, keeping old config")
                new_modules_configs[module_name] = module_info['config']
 
        with CONFIG_LOCK:
            GLOBAL_CONFIG = new_config
            for module_name, new_cfg in new_modules_configs.items():
                if module_name in MODULES:
                    MODULES[module_name]['config'] = new_cfg
 
        logging.info("Configuration reloaded successfully")
        return True
 
    except yaml.YAMLError as e:
        logging.error(f"YAML parsing error in config file: {e}")
        return False
    except Exception as e:
        logging.error(f"Unexpected error during configuration reload: {e}", exc_info=True)
        return False
 
# ----------------------------------------------------------------------
# Фоновый поток-наблюдатель за конфигами
# ----------------------------------------------------------------------
def config_watcher(interval: int):
    if interval == -1:
        return
 
    modules_dir = GLOBAL_CONFIG.get('modules_dir', './modules')
    config_files = get_config_files(modules_dir)
    file_state = {}
    for f in config_files:
        if f.exists():
            file_state[f] = (f.stat().st_mtime, get_file_hash(f))
        else:
            file_state[f] = (None, None)
 
    logging.info(f"Config watcher started with interval {interval}s")
 
    while not STOP_BACKGROUND:
        time.sleep(interval)
        if STOP_BACKGROUND:
            break
 
        changed = False
        for f in config_files:
            if not f.exists():
                if file_state[f] != (None, None):
                    logging.info(f"Config file {f} was deleted")
                    file_state[f] = (None, None)
                    changed = True
                continue
 
            current_mtime = f.stat().st_mtime
            current_hash = get_file_hash(f)
            old_mtime, old_hash = file_state[f]
 
            if current_mtime != old_mtime or current_hash != old_hash:
                logging.info(f"Config file changed: {f} (mtime: {old_mtime}->{current_mtime})")
                file_state[f] = (current_mtime, current_hash)
                changed = True
 
        if changed:
            if interval == 0:
                logging.info("Config change detected, but config_watch_interval=0. No reload performed.")
            else:
                success = reload_configuration()
                if not success:
                    logging.error("Configuration reload failed, keeping old configuration")
 
# ----------------------------------------------------------------------
# Фоновая задача обновления кэша
# ----------------------------------------------------------------------
def background_worker(refresh_interval):
    logging.info("Background worker started")
    while not STOP_BACKGROUND:
        time.sleep(refresh_interval)
        if STOP_BACKGROUND:
            break
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(update_cache)
                future.result(timeout=GLOBAL_CONFIG.get('module_timeout', 30) * 2)
        except concurrent.futures.TimeoutError:
            logging.error("Cache update timed out in background worker")
        except Exception as e:
            logging.error(f"Background update failed: {e}", exc_info=True)
    logging.info("Background worker stopped")
 
# ----------------------------------------------------------------------
# HTTP-сервер (многопоточный)
# ----------------------------------------------------------------------
class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    def __init__(self, server_address, RequestHandlerClass, max_workers=20):
        self.max_workers = max_workers
        self.semaphore = threading.Semaphore(max_workers)
        super().__init__(server_address, RequestHandlerClass)
 
    def process_request(self, request, client_address):
        self.semaphore.acquire()
        try:
            super().process_request(request, client_address)
        finally:
            self.semaphore.release()
 
class SHandler(BaseHTTPRequestHandler):
    # ---------- Основные методы ----------
    def do_GET(self):
        self.handle_request()
 
    def do_POST(self):
        self.handle_request()
 
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
 
    def handle_request(self):
        logging.info(f"Request: {self.command} {self.path}")
        path = self.path.rstrip('/')
 
        if path == '' or path == '/':
            self.handle_root()
        elif path == '/v2':
            self.handle_v2()
        elif path == '/v3':
            self.handle_v3()
        elif path.startswith('/v3/'):
            parts = path.split('/')
            if len(parts) == 3:
                operation = parts[2]
                self.handle_v3_with_operation(operation)
            elif len(parts) >= 4:
                operation = parts[2]
                groups_str = '/'.join(parts[3:])
                self.handle_v3_with_groups(operation, groups_str)
            else:
                self.send_error(404, "Not Found")
        elif path == '/refresh':
            self.handle_refresh()
        else:
            self.send_error(404, "Not Found")
 
    def handle_root(self):
        with CACHE_LOCK:
            cache_copy = copy.deepcopy(CACHE)
        if not cache_copy:
            self.send_json_response([])
            return
        response = []
        for op, op_data in cache_copy.items():
            if op == 'timestamp':
                continue
            if isinstance(op_data, dict):
                for host, value in op_data.items():
                    if isinstance(value, dict):
                        for port, port_value in value.items():
                            response.append({f"{op}.{host}.{port}": port_value})
                    else:
                        response.append({f"{op}.{host}": value})
            else:
                response.append({op: op_data})
        self.send_json_response(response)
 
    def handle_v2(self):
        with CACHE_LOCK:
            cache_copy = copy.deepcopy(CACHE)
        if not cache_copy:
            self.send_json_response([])
            return
        grouped = []
        for op, op_data in cache_copy.items():
            if op == 'timestamp':
                continue
            if isinstance(op_data, dict):
                for host, value in op_data.items():
                    grouped.append({"operation": op, "host": host, "value": value})
            else:
                grouped.append({"operation": op, "host": HOSTNAME, "value": op_data})
        self.send_json_response(grouped)
 
    def handle_v3(self):
        with CACHE_LOCK:
            cache_copy = copy.deepcopy(CACHE)
        if not cache_copy:
            self.send_json_response([])
            return
        response = []
        for op, op_data in cache_copy.items():
            if op == 'timestamp':
                continue
            if isinstance(op_data, dict):
                for host, value in op_data.items():
                    if isinstance(value, dict):
                        for port, port_value in value.items():
                            response.append({
                                "operation": op,
                                "host": host,
                                "value": port_value,
                                "port": str(port)
                            })
                    else:
                        response.append({"operation": op, "host": host, "value": value})
            else:
                response.append({"operation": op, "host": HOSTNAME, "value": op_data})
        self.send_json_response(response)
 
    def handle_v3_with_operation(self, operation):
        with CACHE_LOCK:
            cache_copy = copy.deepcopy(CACHE)
        if not cache_copy or operation not in cache_copy:
            self.send_json_response([])
            return
        op_data = cache_copy[operation]
        response = []
        if isinstance(op_data, dict):
            for host, value in op_data.items():
                if isinstance(value, dict):
                    for port, port_value in value.items():
                        response.append({
                            "operation": operation,
                            "host": host,
                            "value": port_value,
                            "port": str(port)
                        })
                else:
                    response.append({"operation": operation, "host": host, "value": value})
        else:
            response.append({"operation": operation, "host": HOSTNAME, "value": op_data})
        self.send_json_response(response)
 
    def handle_v3_with_groups(self, operation, groups_str):
        with CACHE_LOCK:
            cache_copy = copy.deepcopy(CACHE)
        if not cache_copy or operation not in cache_copy:
            self.send_json_response([])
            return
 
        groups = [g.strip().upper() for g in groups_str.split(',') if g.strip()]
        if not groups:
            self.send_error(400, "Bad Request: empty groups")
            return
        if len(groups) > 10:
            self.send_error(400, "Bad Request: too many groups (max 10)")
            return
 
        op_data = cache_copy[operation]
        if not isinstance(op_data, dict):
            self.send_json_response([])
            return
 
        filtered = {}
        for host_key, value in op_data.items():
            first_label = host_key.split('.')[0] if '.' in host_key else host_key
            upper_label = first_label.upper()
            if any(g in upper_label for g in groups):
                filtered[host_key] = value
 
        if not filtered:
            self.send_json_response([])
            return
 
        response = []
        for host_key, value in filtered.items():
            if isinstance(value, dict):
                for port, port_value in value.items():
                    response.append({
                        "operation": operation,
                        "host": host_key,
                        "value": port_value,
                        "port": str(port)
                    })
            else:
                response.append({"operation": operation, "host": host_key, "value": value})
        self.send_json_response(response)
 
    def handle_refresh(self):
        if self.command not in ('GET', 'POST'):
            self.send_error(405, "Method Not Allowed")
            return
        try:
            update_cache()
            self.send_json_response({"status": "ok", "timestamp": CACHE_TIMESTAMP})
        except Exception as e:
            logging.error(f"Refresh failed: {e}", exc_info=True)
            self.send_error(500, f"Refresh failed: {e}")
 
    def send_json_response(self, data):
        response_json = json.dumps(data, ensure_ascii=False, indent=None)
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(response_json.encode('utf-8'))))
        self.end_headers()
        self.wfile.write(response_json.encode('utf-8'))
 
# ----------------------------------------------------------------------
# Запуск
# ----------------------------------------------------------------------
def print_startup_info():
    print(f"S server version: {VERSION}")
    config_path = Path('config.yaml').absolute()
    print(f"Main config file: {config_path}")
    print(f"Hostname: {HOSTNAME}")
    print("Loaded modules:")
    if MODULES:
        for name, info in MODULES.items():
            cfg = info['config_path'] if info['config_path'] else 'No config file'
            print(f"  - {name} (version {info['version']}, from {info['original_name']}) -> config: {cfg}")
    else:
        print("  No modules loaded")
    refresh = GLOBAL_CONFIG.get('cache', {}).get('refresh_interval', 60)
    timeout = GLOBAL_CONFIG.get('module_timeout', 30)
    max_workers = GLOBAL_CONFIG.get('server', {}).get('max_workers', 20)
    print(f"Cache refresh interval: {refresh} seconds")
    print(f"Module execution timeout: {timeout} seconds")
    print(f"Max parallel requests: {max_workers}")
    print("-" * 60)
 
def main():
    global GLOBAL_CONFIG, MODULES, STOP_BACKGROUND
 
    config_file = Path('config.yaml')
    if not config_file.exists():
        print("Error: config.yaml not found")
        sys.exit(1)
 
    with open(config_file, 'r', encoding='utf-8') as f:
        GLOBAL_CONFIG = yaml.safe_load(f) or {}
 
    setup_logging(GLOBAL_CONFIG)
 
    modules_dir = GLOBAL_CONFIG.get('modules_dir', './modules')
    MODULES = load_modules(modules_dir, GLOBAL_CONFIG)
 
    print_startup_info()
 
    refresh_interval = GLOBAL_CONFIG.get('cache', {}).get('refresh_interval', 60)
    threading.Thread(target=update_cache, daemon=True).start()
 
    bg_thread = threading.Thread(target=background_worker, args=(refresh_interval,), daemon=True)
    bg_thread.start()
 
    # --- НОВЫЙ БЛОК: запуск наблюдателя за конфигами ---
    watch_interval = GLOBAL_CONFIG.get('config_watch_interval', -1)
    if watch_interval < -1:
        logging.warning(f"config_watch_interval={watch_interval} is invalid, setting to -1 (disabled)")
        watch_interval = -1
    elif watch_interval == 0:
        # Для log-only режима используем интервал 60 секунд, чтобы избежать бесконечного цикла без задержки
        watch_interval = 60
        logging.info("Config watcher enabled in log-only mode (config_watch_interval=0 interpreted as 60s)")
    elif watch_interval > 0:
        if watch_interval < 60:
            logging.warning(f"config_watch_interval={watch_interval} is less than 60 seconds, but will be used as is")
        logging.info(f"Config watcher enabled with interval {watch_interval}s")
    if watch_interval >= 0:
        watcher_thread = threading.Thread(target=config_watcher, args=(watch_interval,), daemon=True)
        watcher_thread.start()
    else:
        logging.info("Config watcher disabled (config_watch_interval=-1)")
 
    host = GLOBAL_CONFIG.get('host', '0.0.0.0')
    port = GLOBAL_CONFIG.get('port', 8080)
    max_workers = GLOBAL_CONFIG.get('server', {}).get('max_workers', 20)
    server = ThreadedHTTPServer((host, port), SHandler, max_workers)
    logging.info(f"Starting S server on {host}:{port}")
    print(f"Server is running on {host}:{port} (press Ctrl+C to stop)")
    print("Available endpoints:")
    print("  /           - legacy format (reads cache)")
    print("  /v2         - grouped by host (reads cache)")
    print("  /v3         - separate records (reads cache)")
    print("  /v3/{op}    - operation filter (reads cache)")
    print("  /v3/{op}/{groups} - operation + group filter (reads cache)")
    print("  /refresh    - force cache update (GET or POST)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("Server stopped by user")
        STOP_BACKGROUND = True
        bg_thread.join(timeout=2)
        if watch_interval >= 0:
            watcher_thread.join(timeout=2)
        print("\nServer stopped.")
    finally:
        server.server_close()
 
if __name__ == '__main__':
    main()

