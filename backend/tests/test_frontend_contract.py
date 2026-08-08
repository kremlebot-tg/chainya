from pathlib import Path

import pytest


SOURCE_PATH = Path(__file__).resolve().parents[2] / "src.html"
BUILD_PATH = Path(__file__).resolve().parents[2] / "build.py"
ADMIN_CATALOG_PATH = Path(__file__).resolve().parents[1] / "admin-catalog.html"
LEGAL_CSS_PATH = Path(__file__).resolve().parents[2] / "legal.css"
pytestmark = pytest.mark.skipif(
    not SOURCE_PATH.exists(),
    reason="frontend source is not part of the production backend artifact",
)
SOURCE = SOURCE_PATH.read_text(encoding="utf-8") if SOURCE_PATH.exists() else ""
BUILD_SOURCE = BUILD_PATH.read_text(encoding="utf-8") if BUILD_PATH.exists() else ""
ADMIN_CATALOG = ADMIN_CATALOG_PATH.read_text(encoding="utf-8")
LEGAL_CSS = LEGAL_CSS_PATH.read_text(encoding="utf-8") if LEGAL_CSS_PATH.exists() else ""


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


def test_owner_approved_home_copy_is_preserved():
    assert "Рекомендуем начать свой чайный путь с этих позиций:" in SOURCE
    assert "наполняя пространство Ча Цы" in SOURCE
    assert "Подойдёт для первого знакомства, свидания или дружеской встречи." in SOURCE
    assert "Четыре шага<br>до первого глотка" not in SOURCE


def test_public_pages_use_quiet_account_probe_and_eager_hero_image():
    assert "fetch('/api/account/session'" in SOURCE
    assert "fetch('/api/account',{cache:'no-store'})" not in SOURCE
    assert 'asset_root = "/" if web else ""' in BUILD_SOURCE
    assert 'href="{asset_root}img/tea-baihao.webp"' in BUILD_SOURCE
    assert '.replace(HERO_PRELOAD, "")' in BUILD_SOURCE


def test_catalog_keeps_a_semantic_heading_without_restoring_visual_clutter():
    assert '<h1 class="sr-only" data-i18n="shop_heading">' in SOURCE
    assert "shop_heading:'Каталог китайского чая'" in SOURCE
    assert "teaCard(m, 'h2', true, index < 8)" in SOURCE


def test_display_headings_keep_word_boundaries_across_visual_line_breaks():
    """Visual line breaks must not merge words for crawlers or assistive tech."""

    assert "Чай, <br>к которому <br><em>возвращаются</em>" in SOURCE
    assert "Оптовые поставки и мероприятия <br>для вашего бизнеса" in SOURCE
    assert "Tea <br>you will want <br><em>to return to</em>" in SOURCE


def test_footer_links_keep_minimum_touch_targets():
    assert ".foot a{ min-height:24px; display:inline-flex; align-items:center;" in SOURCE


def test_booking_controls_have_accessible_names_and_heading_order():
    assert '<h2 class="summary__title" data-i18n="sum_h">' in SOURCE
    assert '<span class="sr-only">, ${fullDate}</span>' in SOURCE
    assert 'data-d="${key}" aria-label=' not in SOURCE
    assert '.fmt__d{ font-size:11.5px; color:var(--text-2);' in SOURCE
    assert ".scroll-shell--days{ overflow:hidden; }" in SOURCE
    assert "html,body{ overflow-x:hidden; overflow-x:clip; }" in SOURCE


def test_catalog_price_keeps_a_text_separator_before_its_unit():
    assert "`&nbsp;<small>${T().per_pc}</small>`" in SOURCE
    assert "`&nbsp;<small>${T().per_pack25}</small>`" in SOURCE


def test_catalog_copy_does_not_promise_unverified_effects():
    for unsupported in (
        'за счёт ГАМК, кофеина и L-теанина',
        'Тонизирует и сосредотачивает',
        'Успокаивает и умиротворяет',
        'Эффект мягкий, приземляющий',
        'Скорее успокаивает, чем бодрит',
    ):
        assert unsupported not in SOURCE


def test_business_offer_headings_follow_the_page_heading():
    assert '<h2 data-i18n="b2b_offer1_h">' in SOURCE
    assert '<h2 data-i18n="b2b_offer2_h">' in SOURCE


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


def test_public_catalog_does_not_restore_taste_profile_ui():
    assert 'id="ts-taste"' not in SOURCE
    assert "renderRadar" not in SOURCE
    assert "tea-radar" not in SOURCE


def test_shop_defers_images_below_the_initial_catalog_view():
    assert "function activateCatalogImages()" in SOURCE
    assert "teaCard(m, 'h2', true, index < 8)" in SOURCE
    assert 'data-catalog-src="${m.img}"' in SOURCE
    assert 'data-catalog-eager="1"' in SOURCE
    assert "rect.top > innerHeight + 600" in SOURCE
    assert "if (view === 'shop') activateCatalogImages()" in SOURCE
    assert "if ($('#view-shop').classList.contains('is-active')) activateCatalogImages()" in SOURCE
    assert "$('#shop-empty').hidden = shown !== 0 || curFilter === 'fav';\n    scheduleCatalogImages();" in SOURCE


def test_web_build_uses_root_relative_assets_for_clean_routes():
    assert 'return f"/img/{name}.webp"' in BUILD_SOURCE
    assert 'url(/fonts/{name}.woff2)' in BUILD_SOURCE
    assert '<link rel="icon" href="{asset_root}favicon.png"' in BUILD_SOURCE


def test_admin_catalog_surfaces_incomplete_food_labelling():
    assert 'id="stat-incomplete"' in ADMIN_CATALOG
    assert 'id="catalog-filter"' in ADMIN_CATALOG
    assert "function missingLabelFields(item)" in ADMIN_CATALOG
    assert "Маркировка заполнена не полностью" in ADMIN_CATALOG
    assert "Всё равно показывать товар на сайте?" in ADMIN_CATALOG


def test_mobile_legal_header_has_accessible_touch_targets():
    assert ".brand{min-width:44px;min-height:44px}" in LEGAL_CSS
    assert ".back{display:inline-flex;align-items:center;min-height:44px}" in LEGAL_CSS
    assert ".langs a{display:inline-flex;align-items:center;justify-content:center;min-width:44px;min-height:44px}" in LEGAL_CSS
