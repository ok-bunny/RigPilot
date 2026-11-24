import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_flight_job_headers_share_one_theme_aware_block_component():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")

    assert html.count('class="flight-editor-job-header"') == 2
    assert 'aria-labelledby="flightPrimaryJobTitle"' in html
    assert 'aria-labelledby="flightSecondaryJobTitle"' in html
    assert '<legend><span><b class="flight-editor-job-number">' not in html

    base_rule = css.split(
        ".flight-editor-job > .flight-editor-job-header {", 1
    )[1].split("}", 1)[0]
    assert "box-sizing: border-box" in base_rule
    assert "grid-column: 1 / -1" in base_rule
    assert "border-bottom: 1px solid var(--line)" in base_rule
    assert "background: var(--panel-2)" in base_rule


def test_flight_job_header_remains_sticky_and_compact_in_config_view():
    css = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")
    selector = (
        "#flightForm.flight-miner-config-open > .flight-editor-job.is-config-target "
        "> .flight-editor-job-header {"
    )
    sticky_rule = css.split(selector, 1)[1].split("}", 1)[0]

    assert "position: sticky" in sticky_rule
    assert "top: 0" in sticky_rule
    assert "background: var(--panel-2)" in sticky_rule
    rules = [(re.sub(r"\s+", "", selector), body) for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]
    header = re.sub(r"\s+", "", selector[:-1])
    assert any(key == header + ".flight-miner-config-close-top" and re.search(r"display\s*:\s*grid\s*;", body) for key, body in rules)
    assert any(key == header and re.search(r"padding\s*:\s*12px\s*;", body) for key, body in rules)
