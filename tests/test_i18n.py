import re
from pathlib import Path


def _keys(source: str, language: str) -> set[str]:
    block = re.search(
        rf"{language}: \{{(.*?)\n  \}}",
        source,
        flags=re.DOTALL,
    )
    assert block is not None
    return set(re.findall(r"^\s+([A-Za-z0-9_]+):", block.group(1), re.MULTILINE))


def test_es_and_en_translation_keys_have_full_parity() -> None:
    source = (Path(__file__).parents[1] / "web" / "i18n.js").read_text()
    assert _keys(source, "es") == _keys(source, "en")
