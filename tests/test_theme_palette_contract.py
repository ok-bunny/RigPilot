import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'server' / 'static'


def _variables(block: str) -> dict[str, str]:
    return {match.group('name'): match.group('value').strip()
            for match in re.finditer(r'--(?P<name>[\w-]+):\s*(?P<value>[^;]+);', block)}


def test_single_charcoal_palette_is_the_only_controller_palette():
    styles = (STATIC / 'styles.css').read_text()
    root = re.search(r':root\s*\{(?P<body>.*?)\n\}', styles, re.DOTALL)
    assert root
    values = _variables(root.group('body'))
    assert values['bg'] == '#22252a'
    assert values['panel'] == '#2d3137'
    assert values['sidebar-bg'] == '#1d2025'
    assert values['input-bg'] == '#202328'
    assert values['lime'] == '#f5ad28'
    assert values['chart-cpu'] == '#f5ad28'
    assert values['chart-memory'] == '#70a9e6'
    assert values['green'] == '#8bc85a'
    assert values['gpu-normal'] == '#7fb643'
    assert values['red'] == '#e96b70'
    assert values['radius'] == '6px'

    assert 'color-scheme: dark' in root.group('body')
    assert 'data-theme="light"' not in styles
    assert 'data-theme="system"' not in styles
    assert 'prefers-color-scheme' not in styles
    assert '.theme-button' not in styles


