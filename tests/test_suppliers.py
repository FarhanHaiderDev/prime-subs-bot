"""Fix 1: only implemented/live suppliers are listed, priced and used."""

from dataclasses import asdict

import bot
from conftest import make_callback, new_order


def _pandora_item(cost):
    return {"name": "Netflix 1M", "description": "", "offer": bot.Offer("pandora", "p1", None, cost)}


def _canboso_item(cost):
    return {"name": "netflix 1m", "description": "", "offer": bot.Offer("canboso", "c1", None, cost)}


def test_disabled_canboso_is_not_listed_or_priced(monkeypatch):
    monkeypatch.setattr(bot, "SUPPLIER_API_KEY", "canboso-key")
    monkeypatch.setattr(bot, "CANBOSO_ENABLED", False)

    def canboso_must_not_be_called():
        raise AssertionError("Canboso was queried while disabled")

    monkeypatch.setattr(bot, "fetch_canboso", canboso_must_not_be_called)
    monkeypatch.setattr(bot, "fetch_pandora", lambda: [_pandora_item(3.0)])

    catalog = bot.build_catalog()

    (listing,) = catalog.values()
    assert [o.supplier for o in listing.offers] == ["pandora"]
    assert listing.cost == 3.0
    assert bot.retail_price(listing.cost) == 4.2  # never the (cheaper) Canboso cost


def test_cheaper_canboso_cannot_set_the_price_when_disabled(monkeypatch):
    monkeypatch.setattr(bot, "SUPPLIER_API_KEY", "canboso-key")
    monkeypatch.setattr(bot, "CANBOSO_ENABLED", False)
    monkeypatch.setattr(bot, "fetch_canboso", lambda: [_canboso_item(1.0)])
    monkeypatch.setattr(bot, "fetch_pandora", lambda: [_pandora_item(3.0)])

    (listing,) = bot.build_catalog().values()

    assert listing.cost == 3.0


def test_enabling_the_flag_alone_does_not_make_the_stub_live(monkeypatch):
    monkeypatch.setattr(bot, "SUPPLIER_API_KEY", "canboso-key")
    monkeypatch.setattr(bot, "CANBOSO_ENABLED", True)
    # purchase_canboso is still a stub, so CANBOSO_PURCHASE_IMPLEMENTED stays False
    assert bot.CANBOSO_PURCHASE_IMPLEMENTED is False
    assert bot.supplier_is_live("canboso") is False
    assert bot.supplier_is_live("pandora") is True


async def test_order_completes_when_pandora_is_the_only_live_source(storage, pandora):
    await storage.insert_order(new_order("ord1"))
    callback = make_callback("approve:ord1")

    await bot.approve_order(callback)

    order = await storage.get_order("ord1")
    assert order["status"] == "delivered"
    assert order["outcome"] == "purchased"
    assert order["supplier"] == "pandora"
    assert len(pandora.purchase_posts()) == 1
    assert all("pandoradigital" in url for url in pandora.urls)  # no Canboso request at all
    delivered = callback.bot.texts_to(777)
    assert any("user:pass" in text for text in delivered)


async def test_fulfil_ignores_offers_from_sources_that_are_not_live(storage, pandora, monkeypatch):
    canboso_offer = asdict(bot.Offer("canboso", "c1", None, 1.0))  # cheaper, but not live
    pandora_offer = asdict(bot.Offer("pandora", "p1", None, 3.0))
    await storage.insert_order(new_order("ord2", offers=[canboso_offer, pandora_offer]))

    def canboso_must_not_be_called(*args, **kwargs):
        raise AssertionError("the Canboso purchase was attempted")

    monkeypatch.setitem(bot.PURCHASERS, "canboso", canboso_must_not_be_called)
    callback = make_callback("approve:ord2")

    await bot.approve_order(callback)

    order = await storage.get_order("ord2")
    assert order["status"] == "delivered"
    assert order["supplier"] == "pandora"


async def test_order_with_no_live_offer_is_held_without_any_supplier_call(storage, pandora):
    canboso_only = [asdict(bot.Offer("canboso", "c1", None, 1.0))]
    await storage.insert_order(new_order("ord3", offers=canboso_only, price=1.4))
    callback = make_callback("approve:ord3")

    await bot.approve_order(callback)

    order = await storage.get_order("ord3")
    assert order["status"] == "held"
    assert order["outcome"] == "definitive_failure"
    assert pandora.calls == []
