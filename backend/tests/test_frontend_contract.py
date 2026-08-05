from pathlib import Path

import pytest


SOURCE_PATH = Path(__file__).resolve().parents[2] / "src.html"
BUILD_PATH = Path(__file__).resolve().parents[2] / "build.py"
pytestmark = pytest.mark.skipif(
    not SOURCE_PATH.exists(),
    reason="frontend source is not part of the production backend artifact",
)
SOURCE = SOURCE_PATH.read_text(encoding="utf-8") if SOURCE_PATH.exists() else ""
BUILD_SOURCE = BUILD_PATH.read_text(encoding="utf-8") if BUILD_PATH.exists() else ""


def test_checkout_has_one_clear_payment_method_and_visible_status():
    assert "Карта любого российского банка · Т-Банк" not in SOURCE
    assert 'data-i18n="pay_card">Оплата картой<' in SOURCE
    assert "Оформить заказ · ${a}" in SOURCE
    assert SOURCE.index('id="cart-status"') < SOURCE.index('id="cart-submit"')
    assert "payment-method__mark" not in SOURCE
    assert "fetch('/api/checkout/status'" in SOURCE
    assert "!checkoutAvailable || !getCart().length" in SOURCE
    assert 'id="payment-secure"' in SOURCE


def test_pickup_point_map_and_booking_total_are_rendered():
    assert 'id="c-pvz-map" hidden' in SOURCE
    assert 'id="c-pvz-map-frame"' in SOURCE
    assert 'id="c-pvz-map-link"' in SOURCE
    assert 'id="s-total"' in SOURCE
    assert "fmtPriceValue() * Number(B.guests)" in SOURCE


def test_catalog_explanation_is_short_and_before_the_products():
    note = SOURCE.index('class="shop-note"')
    products = SOURCE.index('id="menu-teas"')
    assert note < products
    assert "Цены на пакеты считаются от цены за 10 г" not in SOURCE
    assert "Вес выбирается в карточке чая" in SOURCE


def test_public_pages_use_quiet_account_probe_and_eager_hero_image():
    assert "fetch('/api/account/session'" in SOURCE
    assert "fetch('/api/account',{cache:'no-store'})" not in SOURCE
    assert 'rel="preload" as="image" href="img/tea-baihao.webp"' in BUILD_SOURCE
    assert '.replace(HERO_PRELOAD, "")' in BUILD_SOURCE


def test_catalog_keeps_a_semantic_heading_without_restoring_visual_clutter():
    assert '<h1 class="sr-only" data-i18n="shop_heading">' in SOURCE
    assert "shop_heading:'Каталог китайского чая'" in SOURCE
    assert "teaCard(m, 'h2')" in SOURCE


def test_booking_controls_have_accessible_names_and_heading_order():
    assert '<h2 class="summary__title" data-i18n="sum_h">' in SOURCE
    assert '<span class="sr-only">, ${fullDate}</span>' in SOURCE
    assert 'data-d="${key}" aria-label=' not in SOURCE
    assert '.fmt__d{ font-size:11.5px; color:var(--text-2);' in SOURCE


def test_catalog_copy_does_not_promise_unverified_effects():
    for unsupported in (
        'за счёт ГАМК, кофеина и L-теанина',
        'Тонизирует и сосредотачивает',
        'Успокаивает и умиротворяет',
        'Эффект мягкий, приземляющий',
        'Скорее успокаивает, чем бодрит',
    ):
        assert unsupported not in SOURCE


def test_language_switch_updates_all_document_metadata():
    assert "function syncDocumentMetadata(view)" in SOURCE
    assert "meta[name=\"description\"]" in SOURCE
    assert "meta[property=\"og:description\"]" in SOURCE
    assert "meta[property=\"og:locale\"]" in SOURCE
    assert "meta[name=\"twitter:description\"]" in SOURCE
    assert "route_descriptions:" in SOURCE


def test_product_sheet_can_render_food_labelling_fields():
    assert 'id="ts-food" hidden' in SOURCE
    assert 'id="ts-food-rows"' in SOURCE
    for field in ("composition", "manufacturer", "shelf_life", "storage"):
        assert f"txt.{field}" in SOURCE
