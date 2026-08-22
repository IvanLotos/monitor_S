import socket
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

VERSION = "1.1.0"
logger = logging.getLogger(__name__)

def _check_single(host: str, port: int, timeout: float, key: str):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return key, True
    except Exception as e:
        logger.debug(f"{key} failed: {e}")
        return key, False

def run(config: dict) -> dict:
    prefix = config.get('prefix', 'portcheck')
    hosts_config = config.get('hosts', [])
    timeout = config.get('timeout', 3)
    max_workers = config.get('max_workers', 30)

    logger.info(f"Starting port check with {len(hosts_config)} hosts, max_workers={max_workers}")

    tasks = []
    for entry in hosts_config:
        if isinstance(entry, dict):
            host = entry.get('host')
            ports = entry.get('ports', [])
        else:
            host = entry
            ports = config.get('ports', [])
        if not host or not ports:
            continue
        first_word = host.split('.')[0] if '.' in host else host
        for port in ports:
            key = f"{prefix}.{first_word}.{port}"
            tasks.append((host, port, timeout, key))

    results = {}
    if tasks:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_key = {
                executor.submit(_check_single, host, port, timeout, key): key
                for host, port, timeout, key in tasks
            }
            for future in as_completed(future_to_key):
                key = future_to_key[future]
                try:
                    _, success = future.result()
                    results[key] = success
                except Exception as e:
                    logger.error(f"Unexpected error for {key}: {e}")
                    results[key] = False

    logger.info(f"Port check finished, {len(results)} results")
    return results