#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Модуль проверки наличия портов на локальном интерфейсе удалённых хостов.
Опрашивает агент мониторинга по HTTPS (mTLS), получает список слушающих портов
на loopback (127.0.0.1, ::1) и сверяет с заданным списком.

Версия: 1.0.3
"""
import json
import logging
import ssl
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager

VERSION = "1.0.3"
logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Кастомный адаптер для mTLS
# ----------------------------------------------------------------------
class MTLSAdapter(HTTPAdapter):
    """HTTPAdapter, настраивающий SSL-контекст для взаимной аутентификации."""

    def __init__(self, cert_file: str, key_file: str, ca_chain_file: str, *args, **kwargs):
        self.cert_file = cert_file
        self.key_file = key_file
        self.ca_chain_file = ca_chain_file
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        context.load_cert_chain(certfile=self.cert_file, keyfile=self.key_file)
        context.load_verify_locations(cafile=self.ca_chain_file)
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True  # явно включаем проверку имени хоста
        kwargs['ssl_context'] = context
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        context.load_cert_chain(certfile=self.cert_file, keyfile=self.key_file)
        context.load_verify_locations(cafile=self.ca_chain_file)
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        kwargs['ssl_context'] = context
        return super().proxy_manager_for(*args, **kwargs)


# ----------------------------------------------------------------------
# Основной класс модуля
# ----------------------------------------------------------------------
class LocalListenerChecker:
    def __init__(self, config: dict):
        self.prefix = config.get('prefix', 'listener')
        self.hosts = config.get('hosts', [])
        self.agent_port = config.get('agent_port', 9500)
        self.cert_path = config.get('cert_path')
        self.key_path = config.get('key_path')
        self.ca_chain_path = config.get('ca_chain_path')
        self.timeout = config.get('timeout', 5)
        self.max_workers = config.get('max_workers', 20)
        self.default_ports = config.get('ports', [])

        if not self.cert_path or not self.key_path or not self.ca_chain_path:
            logger.error("Missing required TLS parameters: cert_path, key_path, ca_chain_path")
            raise ValueError("TLS parameters are required")

        # Глобальная сессия (используется по умолчанию)
        self.session = requests.Session()
        adapter = MTLSAdapter(
            cert_file=self.cert_path,
            key_file=self.key_path,
            ca_chain_file=self.ca_chain_path
        )
        self.session.mount('https://', adapter)
        # НЕ устанавливаем verify=False — полагаемся на адаптер

        logger.info(
            f"LocalListenerChecker initialized: {len(self.hosts)} hosts, "
            f"default_agent_port={self.agent_port}, timeout={self.timeout}s, "
            f"max_workers={self.max_workers}, default_ports={self.default_ports}"
        )

    def _get_ports_for_host(self, host_entry: Dict[str, Any]) -> List[int]:
        return host_entry.get('ports', self.default_ports)

    def _check_single_host(self, host_entry: Dict[str, Any]) -> Dict[str, bool]:
        host = host_entry.get('host')
        if not host:
            logger.warning("Host entry missing 'host' field, skipping")
            return {}

        ports_to_check = self._get_ports_for_host(host_entry)
        if not ports_to_check:
            logger.debug(f"No ports to check for host {host}, skipping")
            return {}

        agent_port = host_entry.get('agent_port', self.agent_port)
        first_label = host.split('.')[0] if '.' in host else host
        url = f"https://{host}:{agent_port}/v1/listeners"

        cert_path = host_entry.get('cert_path', self.cert_path)
        key_path = host_entry.get('key_path', self.key_path)
        ca_chain_path = host_entry.get('ca_chain_path', self.ca_chain_path)

        if (cert_path != self.cert_path or key_path != self.key_path or ca_chain_path != self.ca_chain_path):
            session = requests.Session()
            adapter = MTLSAdapter(
                cert_file=cert_path,
                key_file=key_path,
                ca_chain_file=ca_chain_path
            )
            session.mount('https://', adapter)
            # НЕ устанавливаем verify=False
            logger.debug(f"Using custom TLS certs for host {host}")
        else:
            session = self.session

        try:
            start = time.time()
            response = session.get(url, timeout=self.timeout)
            elapsed = time.time() - start

            if response.status_code != 200:
                logger.warning(
                    f"Agent {host}:{agent_port} returned status {response.status_code} "
                    f"(elapsed {elapsed:.3f}s)"
                )
                return {f"{self.prefix}.{first_label}.{p}": False for p in ports_to_check}

            data = response.json()
            if not data.get('success', False):
                error_msg = data.get('error', 'unknown error')
                logger.warning(f"Agent {host}:{agent_port} reported error: {error_msg}")
                return {f"{self.prefix}.{first_label}.{p}": False for p in ports_to_check}

            agent_ports = {item['port'] for item in data.get('data', []) if isinstance(item, dict) and 'port' in item}
            logger.debug(f"Agent {host}:{agent_port} reported ports: {sorted(agent_ports)}")

            result = {}
            for port in ports_to_check:
                key = f"{self.prefix}.{first_label}.{port}"
                result[key] = port in agent_ports

            logger.info(
                f"Host {host}:{agent_port} checked {len(ports_to_check)} ports, "
                f"found {sum(result.values())} present (elapsed {elapsed:.3f}s)"
            )
            return result

        except requests.exceptions.Timeout:
            logger.error(f"Timeout connecting to agent {host}:{agent_port} (timeout={self.timeout}s)")
        except requests.exceptions.SSLError as e:
            logger.error(f"SSL error for {host}:{agent_port}: {e}")
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error for {host}:{agent_port}: {e}")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON from agent {host}:{agent_port}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error for {host}:{agent_port}: {e}", exc_info=True)

        # Ошибка — все порты False
        return {f"{self.prefix}.{first_label}.{p}": False for p in ports_to_check}

    def run(self) -> Dict[str, bool]:
        if not self.hosts:
            logger.warning("No hosts configured, returning empty result")
            return {}

        results = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_host = {
                executor.submit(self._check_single_host, host_entry): host_entry
                for host_entry in self.hosts
            }
            for future in as_completed(future_to_host):
                host_entry = future_to_host[future]
                try:
                    partial = future.result()
                    results.update(partial)
                except Exception as e:
                    logger.error(f"Unexpected error while processing host {host_entry.get('host')}: {e}")

        logger.info(f"Local listener check finished, total {len(results)} results")
        return results


# ----------------------------------------------------------------------
# Точка входа для ядра S
# ----------------------------------------------------------------------
def run(config: dict) -> dict:
    checker = LocalListenerChecker(config)
    return checker.run()