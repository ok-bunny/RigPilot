import re
from pathlib import Path

STYLES = Path(__file__).resolve().parents[1] / "server/static/styles.css"

def declarations(selector):
    css = re.sub(r"/\*.*?\*/", "", STYLES.read_text(encoding="utf-8"), flags=re.S)
    compact = lambda value: re.sub(r"\s+", "", value)
    values = {}
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if compact(selectors) == compact(selector):
            values.update((key.strip(), value.strip()) for key, value in re.findall(r"([\w-]+)\s*:\s*([^;]+);", body))
    assert values, selector
    return values

def test_inline_grid_fields_keep_controls_top_aligned_when_help_lengths_differ():
    values = declarations(":is(.inline-grid, .device-gpu-oc-grid) > label:not(.toggle-field):not(.source-choice)")
    assert values["min-width"] == "0"
    assert values["align-content"] == "start"
    assert values["grid-auto-rows"] == "max-content"

def test_alignment_rule_does_not_target_checkbox_or_radio_cards():
    for selector in (".toggle-field", ".source-choice"):
        values = declarations(selector)
        assert values["grid-template-columns"] == "auto 1fr"
        assert values["align-items"] == "start"
