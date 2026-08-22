from datetime import datetime

import logging

 

VERSION = "1.0.0"

logger = logging.getLogger(__name__)

 

def run(config: dict) -> dict:

    """

    Возвращает текущее время в заданном формате.

    Параметры конфига:

        key_name: имя ключа (по умолчанию 'timestamp')

        time_format: строка формата strftime (по умолчанию ISO8601: '%Y-%m-%dT%H:%M:%S')

    """

    key_name = config.get('key_name', 'timestamp')

    time_format = config.get('time_format', '%Y-%m-%dT%H:%M:%S')  # ISO8601 без timezone

    now = datetime.now()

    time_str = now.strftime(time_format)

    logger.info(f"Current time: {time_str}")

    return {key_name: time_str}

 

 

 


