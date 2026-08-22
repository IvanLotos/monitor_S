import logging

VERSION = "1.0.0"

logger = logging.getLogger(__name__)

def run(config: dict) -> dict:

    """

    Генерирует заданное число строк key-value.

    Параметры конфига:

        prefix: префикс для ключей (по умолчанию 'dummy')

        count: количество генерируемых записей (по умолчанию 5)

        value_template: шаблон значения (по умолчанию 'dummy_value')

    Ключи имеют вид: {prefix}.{index}, где index начинается с 1.

    """

    prefix = config.get('prefix', 'dummy')

    count = config.get('count', 5)

    value_template = config.get('value_template', 'dummy_value')

    logger.info(f"Generating {count} dummy records with prefix '{prefix}'")

    result = {}

    for i in range(1, count + 1):

        key = f"{prefix}.{i}"

        value = f"{value_template}_{i}"

        result[key] = value

    logger.info(f"Generated {len(result)} dummy keys")

    return result

