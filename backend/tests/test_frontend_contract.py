from pathlib import Path

import pytest


SOURCE_PATH = Path(__file__).resolve().parents[2] / "src.html"
BUILD_PATH = Path(__file__).resolve().parents[2] / "build.py"
ADMIN_CATALOG_PATH = Path(__file__).resolve().parents[1] / "admin-catalog.html"
ADMIN_PATH = Path(__file__).resolve().parents[1] / "admin.html"
ADMIN_GUIDES_PATH = Path(__file__).resolve().parents[1] / "admin-guides.html"
ACCOUNT_PATH = Path(__file__).resolve().parents[1] / "account.html"
LEGAL_CSS_PATH = Path(__file__).resolve().parents[2] / "legal.css"
pytestmark = pytest.mark.skipif(
    not SOURCE_PATH.exists(),
    reason="frontend source is not part of the production backend artifact",
)
SOURCE = SOURCE_PATH.read_text(encoding="utf-8") if SOURCE_PATH.exists() else ""
BUILD_SOURCE = BUILD_PATH.read_text(encoding="utf-8") if BUILD_PATH.exists() else ""
ADMIN_CATALOG = ADMIN_CATALOG_PATH.read_text(encoding="utf-8")
ADMIN_SOURCE = ADMIN_PATH.read_text(encoding="utf-8")
ADMIN_GUIDES = ADMIN_GUIDES_PATH.read_text(encoding="utf-8")
ACCOUNT_SOURCE = ACCOUNT_PATH.read_text(encoding="utf-8")
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


def test_pickup_point_map_link_and_booking_total_are_rendered():
    assert 'id="c-pvz-map" hidden' in SOURCE
    assert 'id="c-pvz-map-address"' in SOURCE
    assert 'id="c-pvz-map-link"' in SOURCE
    assert 'id="c-pvz-map-frame"' not in SOURCE
    assert "$('#c-pvz-map-frame').src" not in SOURCE
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


def test_consent_checkboxes_meet_minimum_touch_target_size():
    assert ".consent input{width:24px;height:24px;" in SOURCE
    assert ".check input{width:24px;height:24px;" in ACCOUNT_SOURCE


def test_account_text_inputs_do_not_trigger_ios_focus_zoom():
    assert "@media(max-width:560px)" in ACCOUNT_SOURCE
    assert ".input{font-size:16px}" in ACCOUNT_SOURCE


def test_account_tabs_support_keyboard_navigation_and_semantic_panels():
    assert 'id="login-tab" role="tab" aria-selected="true" aria-controls="login-form" tabindex="0"' in ACCOUNT_SOURCE
    assert 'id="register-tab" role="tab" aria-selected="false" aria-controls="register-form" tabindex="-1"' in ACCOUNT_SOURCE
    assert 'id="login-form" role="tabpanel" aria-labelledby="login-tab"' in ACCOUNT_SOURCE
    assert 'id="register-form" role="tabpanel" aria-labelledby="register-tab"' in ACCOUNT_SOURCE
    assert "event.key==='ArrowRight'" in ACCOUNT_SOURCE
    assert "event.key==='ArrowLeft'" in ACCOUNT_SOURCE
    assert "event.key==='Home'" in ACCOUNT_SOURCE
    assert "event.key==='End'" in ACCOUNT_SOURCE


def test_account_respects_reduced_motion_preference():
    assert "@media(prefers-reduced-motion:reduce)" in ACCOUNT_SOURCE
    assert "transition-duration:.01ms!important" in ACCOUNT_SOURCE


def test_catalog_admin_distinguishes_saby_price_list_from_base_catalog():
    assert 'id="stat-saby"' in ADMIN_CATALOG
    assert "В каталоге СБИС" in ADMIN_CATALOG
    assert "в прайс-листе сайта" in ADMIN_CATALOG
    assert "В прайс-листе сайта:" in ADMIN_CATALOG
    assert "в основном каталоге:" in ADMIN_CATALOG
    assert "not_in_price_list" in ADMIN_CATALOG
    assert "Сначала добавить в прайс-лист СБИС" in ADMIN_CATALOG


def test_owner_guides_are_searchable_private_help_without_dangerous_actions():
    assert '<meta name="robots" content="noindex,nofollow">' in ADMIN_GUIDES
    assert 'id="guide-search"' in ADMIN_GUIDES
    assert "СБИС: каталог и чеки покупок" in ADMIN_GUIDES
    assert "Заказ отправлен/выдан · создать чек" in ADMIN_GUIDES
    assert "Предоплата, выдача и складской учёт" in ADMIN_GUIDES
    assert "Простая смена статуса не создаёт окончательный чек" in ADMIN_GUIDES
    assert "Сверка каталога работает только на чтение" in ADMIN_GUIDES
    assert "нельзя включать поверх онлайн-чека Т‑Банка" in ADMIN_GUIDES
    assert "Создание отправления — отдельная реальная запись" in ADMIN_GUIDES
    assert "method:'DELETE'" in ADMIN_GUIDES
    assert "api/admin/refund" not in ADMIN_GUIDES
    assert "api/admin/cdek" not in ADMIN_GUIDES


def test_admin_surfaces_saby_order_readiness_on_every_view():
    alert = ADMIN_SOURCE.index('id="saby-orders-attention"')
    overview = ADMIN_SOURCE.index('id="view-overview"')
    assert alert < overview
    assert "Saby · Retail" in ADMIN_SOURCE
    assert "Saby · Delivery" in ADMIN_SOURCE
    assert "Saby · заказы" in ADMIN_SOURCE
    assert "Saby · способ учёта покупки" in ADMIN_SOURCE
    assert "Передача покупки в Saby безопасно заблокирована" in ADMIN_SOURCE
    assert "Заказы не передаются в Saby" in ADMIN_SOURCE
    assert "X-Chainya-Admin':'saby-readiness'" in ADMIN_SOURCE
    assert "loadSabyReadiness(true)" in ADMIN_SOURCE


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
    assert "function missingPublicationFields(item)" in ADMIN_CATALOG
    assert "Перед публикацией заполните карточку" in ADMIN_CATALOG
    assert "Всё равно показывать товар на сайте?" not in ADMIN_CATALOG


def test_admin_button_like_links_share_the_button_alignment_contract():
    assert "display:inline-flex;align-items:center;justify-content:center;gap:7px;line-height:1.2;text-align:center;text-decoration:none" in ADMIN_CATALOG
    assert '<a class="btn" href="/manage">Обзор</a>' in ADMIN_CATALOG
    assert '<a class="btn" href="/shop" target="_blank" rel="noopener">Открыть магазин ↗</a>' in ADMIN_CATALOG
    assert "display:inline-flex;align-items:center;justify-content:center;line-height:1.2;text-align:center;text-decoration:none" in ADMIN_SOURCE


def test_admin_secondary_controls_are_visually_centered():
    assert ".move{width:30px;height:28px;border:1px solid var(--line);background:transparent;color:var(--muted);display:grid;place-items:center;padding:0;line-height:1}" in ADMIN_CATALOG
    assert ".dialog-close{width:42px;height:42px;border:1px solid var(--line2);background:transparent;color:var(--text);font-size:22px;display:grid;place-items:center;padding:0;line-height:1}" in ADMIN_CATALOG
    assert ".period__button{height:35px;min-width:54px;border-right:0;color:var(--muted);font-size:12px;display:inline-flex;align-items:center;justify-content:center;line-height:1.2;text-align:center}" in ADMIN_SOURCE
    assert ".owner-tools__actions .btn:first-child,.owner-tools__actions .btn:last-child{grid-column:1/-1}" in ADMIN_CATALOG


def test_catalog_escapes_owner_controlled_copy_before_using_inner_html():
    assert "const escHtml = value =>" in SOURCE
    assert '<${headingTag} class="tea__name">${escHtml(txt.n)}</${headingTag}>' in SOURCE
    assert '<p class="tea__orig">${escHtml(txt.o)}</p>' in SOURCE
    assert '<div class="citem__name">${escHtml(T().teas[it.id].n)}</div>' in SOURCE
    assert 'aria-label="${escAttr(T().cart_decrease' in SOURCE


def test_saby_review_distinguishes_unknown_stock_from_out_of_stock():
    assert "item.suggested_stock===null?'остаток не определён'" in ADMIN_CATALOG
    assert "товар безопасно отмечен как недоступный" in ADMIN_CATALOG


def test_mobile_legal_header_has_accessible_touch_targets():
    assert ".brand{min-width:44px;min-height:44px}" in LEGAL_CSS
    assert ".back{display:inline-flex;align-items:center;min-height:44px}" in LEGAL_CSS
    assert ".langs a{display:inline-flex;align-items:center;justify-content:center;min-width:44px;min-height:44px}" in LEGAL_CSS
