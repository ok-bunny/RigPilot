from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_flight_card_headers_follow_the_shared_data_column_order():
    html = (ROOT / "server/static/index.html").read_text(encoding="utf-8")
    table_head = html.split('class="table-wrap flight-card-wrap"', 1)[1].split(
        "</thead>", 1
    )[0]
    headings = ["名称", "币种", "挖矿软件", "钱包", "矿池", "使用设备"]
    positions = [table_head.index(f"<th>{heading}</th>") for heading in headings]
    assert positions == sorted(positions)
    assert "主挖钱包" not in table_head
    assert "主挖矿池" not in table_head
    app = (ROOT / "server/static/app.js").read_text(encoding="utf-8")
    assert "openFlightDialog(button.dataset.editFlight)" in app
    assert 'deleteResource("flight", button.dataset.deleteFlight)' in app


def test_single_and_dual_cards_keep_responsive_aligned_tracks():
    # Actual card projection, escaping, roles and permissions are exercised by
    # hiveos_flight_ui.test.cjs; this contract owns the CSS layout only.
    css = (ROOT / "server/static/styles.css").read_text(encoding="utf-8")
    assert (
        'grid-template-areas: "title title title actions actions" "coin miner wallet pool devices"'
    ) in css
    assert (
        ".flight-sheet-card.has-secondary .flight-card-leg-list { "
        "grid-template-rows: repeat(2, minmax(38px, 1fr)); }"
    ) in css
    assert (
        'grid-template-areas: "title actions" "coin miner" "wallet pool" '
        '"devices devices"'
    ) in css
    assert "#flightsPage .flight-card-actions { display: flex !important; min-height: 56px; grid-area: actions;" in css
    assert "overflow-wrap: anywhere; white-space: normal; word-break: break-word;" in css
