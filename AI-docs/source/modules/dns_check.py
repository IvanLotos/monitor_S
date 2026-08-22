import socket
import logging

VERSION = "1.1.0"
logger = logging.getLogger(__name__)

def run(config: dict) -> dict:
    prefix = config.get('prefix', 'dns')
    hosts = config.get('hosts', [])
    timeout = config.get('timeout', 5)
    results = {}

    logger.info(f"Starting DNS check for {len(hosts)} hosts")

    for entry in hosts:
        fqdn = entry.get('fqdn')
        expected_ip = entry.get('ip')
        if not fqdn or not expected_ip:
            logger.warning(f"Skipping invalid entry: {entry}")
            continue

        first_word = fqdn.split('.')[0] if '.' in fqdn else fqdn
        key = f"{prefix}.{first_word}"

        try:
            resolved_ips = socket.gethostbyname_ex(fqdn)[2]
            is_correct = expected_ip in resolved_ips
            logger.debug(f"{fqdn} -> {resolved_ips}, expected {expected_ip}: {is_correct}")
        except socket.gaierror:
            logger.debug(f"{fqdn} could not be resolved")
            is_correct = False
        except Exception as e:
            logger.error(f"Unexpected error for {fqdn}: {e}")
            is_correct = False

        results[key] = is_correct

    logger.info(f"DNS check finished, {len(results)} results")
    return results