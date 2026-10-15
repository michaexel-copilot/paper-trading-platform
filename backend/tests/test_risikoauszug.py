"""B-11/HED-83 -- maschinenlesbarer Tages-Risikoauszug.

Integrationstests gegen eine echte (SQLite-)Datenbank und gescriptete Marktdaten,
derselbe Aufbau wie tests/test_stufe1.py.
"""

from datetime import UTC, datetime, timedelta

from app.models import Order, Portfolio, Position, ValueSnapshot
from app.trading.risikoauszug import _NUR_ORDERZEITPUNKT, erstelle_tagesauszug
from tests.conftest import D, asset_id, create_portfolio


async def _portfolio(client, session, **kwargs) -> Portfolio:
    portfolio_id = (await create_portfolio(client, currency="EUR", cash="10000", **kwargs))["id"]
    return await session.get(Portfolio, portfolio_id)


async def test_frisches_portfolio_ohne_positionen(session, market, prices, client):
    portfolio = await _portfolio(client, session)

    auszug = await erstelle_tagesauszug(
        session, market, portfolio, tranche="B1", tranche_eur=D(2000)
    )

    assert auszug.nav == D(10000)
    assert auszug.cash_boerse == D(10000)
    assert auszug.cash_ausserhalb_boerse == D(0)
    assert auszug.cash_gesamt == D(10000)
    assert auszug.barquote_pct == D(100)
    assert auszug.anzahl_offener_positionen == 0
    assert auszug.positionen_ohne_stop == []
    # Kein Tagesanfangs-Schnappschuss vor heute -- Tages-P&L nicht berechenbar.
    assert auszug.pnl.berechenbar is False
    assert auszug.pnl.tages_pnl_gesamt is None


async def test_mindest_barbestand_erscheint_in_den_limits_und_besteht(
    session, market, prices, client
):
    portfolio = await _portfolio(client, session)

    auszug = await erstelle_tagesauszug(
        session, market, portfolio, tranche="B1", tranche_eur=D(2000)
    )

    zeile = next(e for e in auszug.limits if e.limit == "mindest_barbestand")
    assert zeile.berechenbar is True
    assert zeile.gebrochen is False  # 10.000 € cash, weit über 3.000 €/30% NAV


async def test_orderzeitpunkt_limits_sind_nicht_im_tagesauszug(session, market, prices, client):
    portfolio = await _portfolio(client, session)

    auszug = await erstelle_tagesauszug(
        session, market, portfolio, tranche="B1", tranche_eur=D(2000)
    )

    namen = {e.limit for e in auszug.limits}
    assert namen.isdisjoint(_NUR_ORDERZEITPUNKT)


async def test_position_ohne_stop_wird_gemeldet(session, market, prices, client):
    portfolio = await _portfolio(client, session)
    btc = await asset_id(session, "BTC")
    session.add(Position(portfolio_id=portfolio.id, asset_id=btc, quantity=D("0.01"),
                         avg_cost=D(50000)))
    await session.commit()

    auszug = await erstelle_tagesauszug(
        session, market, portfolio, tranche="B1", tranche_eur=D(2000)
    )

    assert auszug.anzahl_offener_positionen == 1
    assert auszug.positionen_ohne_stop == ["BTC"]


async def test_position_mit_offenem_stop_wird_nicht_gemeldet(session, market, prices, client):
    portfolio = await _portfolio(client, session)
    btc = await asset_id(session, "BTC")
    session.add(Position(portfolio_id=portfolio.id, asset_id=btc, quantity=D("0.01"),
                         avg_cost=D(50000)))
    session.add(Order(
        portfolio_id=portfolio.id, asset_id=btc, client_order_id="stop-1", side="sell",
        type="stop", quantity=D("0.01"), stop_price=D(47500), status="open",
    ))
    await session.commit()

    auszug = await erstelle_tagesauszug(
        session, market, portfolio, tranche="B1", tranche_eur=D(2000)
    )

    assert auszug.positionen_ohne_stop == []


async def test_usd_usdt_usdc_anteil_getrennt_ausgewiesen(session, market, sources, clock, client):
    sources["okx"].add("BTC/USDC", currency="USDC")
    sources["okx"].price("BTC/USDC", 50000)
    # ETHs echtes okx-Quellsymbol ist seit HED-99 ETH/USDC (kein USDT-Paar mehr
    # verfügbar) -- der Test erzwingt hier testweise eine USDT-Quote über dasselbe
    # Symbol, um die Bucket-Logik nach ``Quote.currency`` zu prüfen, unabhängig
    # davon, ob ein echtes USDT-Paar existiert.
    sources["okx"].add("ETH/USDC", currency="USDT")
    sources["okx"].price("ETH/USDC", 2000, currency="USDT")
    sources["yahoo"].add("EURUSD=X", "currency", exchange="CCY")
    sources["yahoo"].price("EURUSD=X", "1.0")

    portfolio = await _portfolio(client, session)
    btc = await asset_id(session, "BTC")
    eth = await asset_id(session, "ETH")
    # 0.1 BTC @ 50.000 USDC = 5.000 USDC = 5.000 EUR (fx 1:1); 1 ETH @ 2.000 USDT =
    # 2.000 USDT = 2.000 EUR.
    session.add(Position(portfolio_id=portfolio.id, asset_id=btc, quantity=D("0.1"),
                         avg_cost=D(50000)))
    session.add(Position(portfolio_id=portfolio.id, asset_id=eth, quantity=D("1"),
                         avg_cost=D(2000)))
    await session.commit()

    auszug = await erstelle_tagesauszug(
        session, market, portfolio, tranche="B1", tranche_eur=D(2000)
    )

    # nav = 10.000 cash (unverändert, keine echten Trades) + 5.000 + 2.000 = 17.000
    assert auszug.nav == D(17000)
    assert auszug.usdc_anteil_pct == (D(5000) / D(17000) * 100)
    assert auszug.usdt_anteil_pct == (D(2000) / D(17000) * 100)
    assert auszug.usd_anteil_pct == D(0)


async def test_tages_pnl_aufteilung_bei_reiner_fx_bewegung(
    session, market, sources, clock, client
):
    """Bewegt sich nur der FX-Kurs (Preis in Quote-Währung unverändert), muss die
    gesamte Tages-P&L als Währungseffekt erscheinen, das Handelsergebnis bei 0
    liegen -- und beide Teile müssen exakt die Gesamtsumme ergeben (Konstruktion
    in app.trading.risikoauszug._pnl_aufteilung: Handelsergebnis ist der Rest)."""
    sources["okx"].add("BTC/USDC", currency="USDC")
    sources["okx"].price("BTC/USDC", 50000)
    sources["yahoo"].add("EURUSD=X", "currency", exchange="CCY")
    sources["yahoo"].price("EURUSD=X", "1.0")  # fx(USDC->EUR) heute = 1.0

    portfolio = await _portfolio(client, session)
    btc = await asset_id(session, "BTC")
    session.add(Position(portfolio_id=portfolio.id, asset_id=btc, quantity=D("0.1"),
                         avg_cost=D(50000)))

    day_start = datetime.combine(clock.current.date(), datetime.min.time(), tzinfo=UTC)
    gestern = day_start - timedelta(hours=12)
    # Tagesanfangs-Schnappschuss: fx(USDC->EUR) war 0.8 -- Position damals
    # 0,1 * 50.000 * 0,8 = 4.000 EUR wert, NAV damals 10.000 (cash) + 4.000 = 14.000.
    session.add(ValueSnapshot(
        portfolio_id=portfolio.id, at=gestern, value=D(14000), kind="daily",
        breakdown={"positions": {str(btc): {
            "quantity": "0.1", "price_quote": "50000", "quote_currency": "USDC",
            "fx_rate": "0.8",
        }}},
    ))
    await session.commit()

    auszug = await erstelle_tagesauszug(
        session, market, portfolio, tranche="B1", tranche_eur=D(2000)
    )

    # Heute: Position 0,1 * 50.000 * 1,0 = 5.000 EUR, NAV = 10.000 + 5.000 = 15.000.
    assert auszug.nav == D(15000)
    assert auszug.pnl.berechenbar is True
    assert auszug.pnl.tages_pnl_gesamt == D(1000)
    assert auszug.pnl.waehrungseffekt == D(1000)
    assert auszug.pnl.handelsergebnis == D(0)
    assert auszug.pnl.handelsergebnis + auszug.pnl.waehrungseffekt == auszug.pnl.tages_pnl_gesamt


async def test_externe_reserve_zaehlt_zu_cash_gesamt_und_barquote(session, market, prices, client):
    portfolio = await _portfolio(client, session)
    portfolio.external_reserve_cash = D(5000)
    await session.commit()

    auszug = await erstelle_tagesauszug(
        session, market, portfolio, tranche="B1", tranche_eur=D(2000)
    )

    assert auszug.nav == D(10000)  # keine Positionen -- NAV ist reines Cash
    assert auszug.cash_boerse == D(10000)
    assert auszug.cash_ausserhalb_boerse == D(5000)
    assert auszug.cash_gesamt == D(15000)
    assert auszug.barquote_pct == (D(15000) / D(10000) * 100)


async def test_api_endpoint_liefert_den_auszug(client, prices):
    portfolio = await create_portfolio(client, currency="EUR", cash="10000")

    response = await client.get(f"/api/portfolios/{portfolio['id']}/risikoauszug")

    assert response.status_code == 200
    body = response.json()
    assert body["portfolio_id"] == portfolio["id"]
    assert body["nav"] == "10000"
    assert body["tages_pnl_berechenbar"] is False
    assert any(zeile["limit"] == "mindest_barbestand" for zeile in body["limits"])


async def test_to_json_ist_vollstaendig_serialisierbar(session, market, prices, client):
    import json

    portfolio = await _portfolio(client, session)
    auszug = await erstelle_tagesauszug(
        session, market, portfolio, tranche="B1", tranche_eur=D(2000)
    )

    serialisiert = json.dumps(auszug.to_json())
    wieder = json.loads(serialisiert)
    assert D(wieder["nav"]) == D(10000)
    assert wieder["portfolio_id"] == portfolio.id
