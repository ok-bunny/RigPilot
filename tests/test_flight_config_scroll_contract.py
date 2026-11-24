from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_regular_flight_editor_uses_the_dynamic_viewport_as_its_scrollport():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")
    form_rule = css.split(".modal-flight .modal-card {", 1)[1].split("}", 1)[0]
    footer_rules = css.split(
        "#flightForm:not(.flight-miner-config-open) > .modal-actions:last-child {"
    )[1:]
    footer_rule = next(
        rule.split("}", 1)[0]
        for rule in footer_rules
        if "position: sticky" in rule.split("}", 1)[0]
    )

    viewport_fallbacks = [
        "max-height: calc(100vh - 30px)",
        "max-height: calc(100svh - 30px)",
        "max-height: calc(100dvh - 30px)",
    ]
    assert all(value in form_rule for value in viewport_fallbacks)
    assert [form_rule.index(value) for value in viewport_fallbacks] == sorted(
        form_rule.index(value) for value in viewport_fallbacks
    )
    assert "overflow-x: hidden" in form_rule
    assert "overflow-y: auto" in form_rule
    assert "overscroll-behavior-y: contain" in form_rule
    assert "scrollbar-gutter: stable" in form_rule
    assert "-webkit-overflow-scrolling: touch" in form_rule
    regular_form_rules = css.split("#flightForm:not(.flight-miner-config-open) {")[1:]
    regular_form_rule = next(
        rule.split("}", 1)[0]
        for rule in regular_form_rules
        if "scroll-padding-bottom" in rule.split("}", 1)[0]
    )
    assert "padding-bottom: 0" in regular_form_rule
    assert "scroll-padding-bottom: calc(84px + env(safe-area-inset-bottom))" in css
    assert "position: sticky" in footer_rule
    assert "bottom: 0" in footer_rule
    assert "env(safe-area-inset-bottom)" in footer_rule
    assert (
        '<button id="createFlightButton" class="btn btn-primary" type="submit">保存飞行表</button>'
        in html
    )


def test_flight_miner_configuration_has_a_bounded_scrollport():
    app = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")
    form_rule = css.split("#flightForm.flight-miner-config-open {", 1)[1].split("}", 1)[0]
    target_rules = css.split(
        "#flightForm.flight-miner-config-open > .flight-editor-job.is-config-target {"
    )[1:]
    target_rule = next(
        rule.split("}", 1)[0] for rule in target_rules if "position: static" in rule.split("}", 1)[0]
    )
    footer_rules = css.split(
        "#flightForm.flight-miner-config-open > .flight-editor-job.is-config-target > .flight-miner-config-actions {"
    )[1:]
    footer_rule = next(
        rule.split("}", 1)[0]
        for rule in footer_rules
        if "position: sticky" in rule.split("}", 1)[0]
    )

    assert "display: block" in form_rule
    assert "#flightDialog { overflow: hidden; }" in css
    assert "#flightForm.flight-miner-config-open .gpu-settings { max-height: none; overflow: visible" in css
    assert "overflow-y: auto" in form_rule
    assert "scrollbar-gutter: stable" in form_rule
    assert "-webkit-overflow-scrolling: touch" in form_rule
    assert "min-height: 100%" in target_rule
    assert "max-height: none" in target_rule
    assert "overflow: visible" in target_rule
    assert "overflow-y: auto" not in target_rule
    assert "bottom: 0" in footer_rule
    opener = app.split("function openFlightMinerConfig(prefix)", 1)[1].split(
        "function reportFlightInvalidControl", 1
    )[0]
    assert "form.scrollTop = 0" in opener
    assert "fieldset.scrollTop = 0" not in opener
    assert all(
        "overflow-y: auto" not in rule.split("}", 1)[0]
        for rule in target_rules
    )
