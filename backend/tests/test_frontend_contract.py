from pathlib import Path

import pytest

SOURCE_PATH = Path(__file__).resolve().parents[2] / "src.html"
BUILD_PATH = Path(__file__).resolve().parents[2] / "build.py"
ADMIN_CATALOG_PATH = Path(__file__).resolve().parents[1] / "admin-catalog.html"
ADMIN_CATALOG_JS_PATH = Path(__file__).resolve().parents[1] / "admin-catalog.js"
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
ADMIN_CATALOG_JS = ADMIN_CATALOG_JS_PATH.read_text(encoding="utf-8")
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
    assert 'id="c-name" data-i18n-ph="f_name_ph" autocomplete="given-name" required' in SOURCE
    assert "requireCartField('#c-name',T().cart_name_required)" in SOURCE


def test_checkout_explains_stock_and_ambiguous_bank_failures_without_duplicate_payment():
    assert "cart_stock_unavailable:'Одного из товаров уже недостаточно" in SOURCE
    assert "cart_payment_checking:'Ответ банка не получен" in SOURCE
    assert "response.status === 409" in SOURCE
    assert "response.status === 502" in SOURCE
    assert "savedAttempt.recovery === true" in SOURCE
    assert "saveCheckoutAttempt(" in SOURCE
    assert "T().cart_payment_checking" in SOURCE


def test_admin_orders_explain_local_stock_reservation_state():
    assert "Товар зарезервирован" in ADMIN_SOURCE
    assert "Резерв истёк" in ADMIN_SOURCE
    assert "Резерв до списания Saby" in ADMIN_SOURCE
    assert "Остаток Saby обновлён" in ADMIN_SOURCE
    assert "Резерв освобождён" in ADMIN_SOURCE
    assert "Резерв: нужна проверка" in ADMIN_SOURCE


def test_admin_prioritizes_payment_reconciliation_incidents():
    assert "payment_reconciliation_incidents" in ADMIN_SOURCE
    assert "Платёжные операции требуют сверки" in ADMIN_SOURCE
    assert "создание платежа: нужна проверка" in ADMIN_SOURCE
    assert "частичный возврат: нужна сверка" in ADMIN_SOURCE
    assert "Не создавайте повторный платёж или возврат" in ADMIN_SOURCE
    assert "'init_ambiguous','capture_ambiguous','refund_ambiguous','partially_refunded'" in ADMIN_SOURCE
    assert "Сайт не создаёт чек возврата и не меняет склад автоматически" in ADMIN_GUIDES


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
    assert "preload_hero: bool = True" in BUILD_SOURCE
    assert "preload_hero=False" in BUILD_SOURCE


def test_catalog_keeps_a_semantic_heading_without_restoring_visual_clutter():
    assert '<h1 class="shop-heading" id="shop-heading" data-i18n="shop_heading">' in SOURCE
    assert "shop_heading:'Каталог китайского чая'" in SOURCE
    assert "teaCard(m, 'h2', true, index < 8)" in SOURCE


def test_teaware_has_a_separate_public_navigation_route():
    assert SOURCE.count('href="/teaware" data-go="teaware" data-i18n="nav_teaware"') == 2
    assert "const VIEW_PATHS = { home:'/', shop:'/shop', teaware:'/teaware'" in SOURCE
    assert "catalogGroup = view === 'teaware' ? 'teaware' : 'tea'" in SOURCE
    assert "curFilter = catalogGroup" in SOURCE
    assert "heading.dataset.i18n = view === 'teaware' ? 'teaware_heading' : 'shop_heading'" in SOURCE
    assert "search.dataset.i18nPh = view === 'teaware' ? 'teaware_search_ph' : 'shop_search_ph'" in SOURCE
    assert "$('#shop-note').hidden = view === 'teaware'" in SOURCE
    assert "filter_all_tea:'Все чаи'" in SOURCE
    assert "filter_all_teaware:'Вся посуда'" in SOURCE
    assert "['all','tea','teaware'].forEach" not in SOURCE
    assert "typeGroup(c.dataset.type) === catalogGroup" in SOURCE
    assert "type.group === catalogGroup && TEA_META.some(item => item.t === type.id)" in SOURCE
    assert "empty.dataset.i18n = sectionEmpty ? 'catalog_empty' : 'shop_empty'" in SOURCE
    assert "$('#filters-shell').hidden = sectionEmpty" in SOURCE
    assert "nav_teaware:'Посуда'" in SOURCE
    assert "nav_teaware:'Teaware'" in SOURCE
    assert "nav_teaware:'茶具'" in SOURCE
    assert "nav_shop:'Чай'" in SOURCE
    assert "nav_shop:'Tea'" in SOURCE
    assert "nav_shop:'茶'" in SOURCE
    assert 'data-i18n="nav_shop">Купить чай</a>' not in SOURCE


def test_catalog_cards_expose_indexable_product_links_and_keep_modal_navigation():
    assert 'href="${productRoute(m.id)}"' in SOURCE
    assert "function productRoute(id, language=LANG)" in SOURCE
    assert "typeGroup(product.t) === 'teaware' ? 'teaware' : 'tea'" in SOURCE
    assert "event.preventDefault(); lastCard = hit; openTea(m);" in SOURCE
    assert "location.pathname.match(/^\\/(?:(en|zh)\\/)?(tea|teaware)\\/" in SOURCE
    assert "history.pushState({ tea:m.id }, '', productRoute(m.id))" in SOURCE
    assert "syncProductMetadata(m, txt)" in SOURCE
    assert "setMeta('meta[property=\"og:type\"]', 'website')" in SOURCE


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
    assert "в прайс-листе сайта" in ADMIN_CATALOG_JS
    assert "В прайс-листе сайта:" in ADMIN_CATALOG_JS
    assert "в основном каталоге:" in ADMIN_CATALOG_JS
    assert "not_in_price_list" in ADMIN_CATALOG_JS
    assert "Сначала добавить в прайс-лист СБИС" in ADMIN_CATALOG_JS


def test_catalog_admin_links_to_the_correct_public_product_section():
    assert "itemGroup(draft) === 'teaware' ? 'teaware' : 'tea'" in ADMIN_CATALOG_JS
    assert "open.href = `/${section}/${encodeURIComponent(draft.id)}`" in ADMIN_CATALOG_JS
    assert "Дополнить РУ" not in ADMIN_CATALOG_JS
    assert "Дополнить · ${missingCount}" in ADMIN_CATALOG_JS
    assert ".editor>.empty{align-items:start;padding-top:0}" in ADMIN_CATALOG


def test_owner_guides_are_searchable_private_help_without_dangerous_actions():
    assert '<meta name="robots" content="noindex,nofollow">' in ADMIN_GUIDES
    assert 'id="guide-search"' in ADMIN_GUIDES
    assert "СБИС: каталог и чеки покупок" in ADMIN_GUIDES
    assert "Один чек интернет-заказа" in ADMIN_GUIDES
    assert "Один чек, продажа и складской учёт" in ADMIN_GUIDES
    assert "Успех подтверждается только настоящим фискальным признаком" in ADMIN_GUIDES
    assert "После подтверждённого чека проверьте, что товар списался" in ADMIN_GUIDES
    assert "Сверка каталога работает только на чтение" in ADMIN_GUIDES
    assert "нельзя включать поверх онлайн-чека Т‑Банка" in ADMIN_GUIDES
    assert "Создание отправления — отдельная реальная запись" in ADMIN_GUIDES
    assert "Оплата одностадийная" in ADMIN_GUIDES
    assert "число таких заказов система сейчас не ограничивает" in ADMIN_GUIDES
    assert "Банк сначала блокирует сумму" not in ADMIN_GUIDES
    assert "автоматически закрывает приём новых платежей" not in ADMIN_GUIDES
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
    assert "Saby · фискальные чеки" in ADMIN_SOURCE
    assert "Saby принял операции, ждём кассу или ОФД" in ADMIN_SOURCE
    assert "принят, ожидает кассу или ОФД" in ADMIN_SOURCE
    assert "Saby · способ учёта покупки" in ADMIN_SOURCE
    assert "Передача покупки в Saby безопасно заблокирована" in ADMIN_SOURCE
    assert "Заказы не передаются в Saby" in ADMIN_SOURCE
    assert "X-Chainya-Admin':'saby-readiness'" in ADMIN_SOURCE
    assert "loadSabyReadiness(true)" in ADMIN_SOURCE
    assert "sabyReadiness?.fiscal_probe" in ADMIN_SOURCE
    assert "Кассовый контур Saby требует настройки" in ADMIN_SOURCE
    assert "company_not_found" in ADMIN_SOURCE
    assert "separate_auth_required" in ADMIN_SOURCE
    assert "ККТ проверяется отдельно" in ADMIN_SOURCE
    assert "физическая ККТ проверяется отдельно" in ADMIN_SOURCE


def test_admin_tbank_recovery_is_explicit_and_fail_closed():
    assert "/tbank/status" in ADMIN_SOURCE
    assert "/tbank/reconcile" in ADMIN_SOURCE
    assert "X-Chainya-Admin':'tbank-reconcile'" in ADMIN_SOURCE
    assert "Восстановить статус" in ADMIN_SOURCE
    assert "Нового платежа или возврата не будет" in ADMIN_SOURCE
    assert "identity_matches" in ADMIN_SOURCE
    assert "amount_matches" in ADMIN_SOURCE


def test_admin_saby_receipt_check_reads_only_a_known_receipt():
    assert "/saby/receipts/${kind}/check" in ADMIN_SOURCE
    assert "X-Chainya-Admin':'saby-receipt-check'" in ADMIN_SOURCE
    assert "Проверить чек Saby" in ADMIN_SOURCE
    assert "current?.id" in ADMIN_SOURCE
    assert "новой продажи не будет" in ADMIN_SOURCE


def test_booking_controls_have_accessible_names_and_heading_order():
    assert '<h2 class="summary__title" data-i18n="sum_h">' in SOURCE
    assert '<span class="sr-only">, ${fullDate}</span>' in SOURCE
    assert 'data-d="${key}" aria-label=' not in SOURCE
    assert '.fmt__d{ font-size:12.5px; color:var(--text-2);' in SOURCE
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
    assert "if (CATALOG_VIEWS.has(view)) activateCatalogImages()" in SOURCE
    assert "if ($('#view-shop').classList.contains('is-active')) activateCatalogImages()" in SOURCE
    assert "empty.hidden = shown !== 0 || curFilter === 'fav';\n    scheduleCatalogImages();" in SOURCE


def test_initial_catalog_waits_for_live_data_before_showing_product_photos():
    """Stale embedded photos must not flash before the owner-managed catalog loads."""

    assert "let catalogReady = false" in SOURCE
    assert "function renderCatalogLoading()" in SOURCE
    assert "hero.removeAttribute('src')" in SOURCE
    assert "home.replaceChildren(...skeleton(3))" in SOURCE
    assert "menu.replaceChildren(...skeleton(8))" in SOURCE
    assert "const firstResolution = !catalogReady" in SOURCE
    assert "if (!catalogReady){ go('shop', false); return; }" in SOURCE
    assert "if (!catalogReady){\n          catalogReady = true;\n          renderResolvedCatalog();" in SOURCE
    assert "if (catalogReady){\n      selectHeroImage();\n      buildRange();\n      buildTeaGrids();" in SOURCE
    assert "document.currentScript.previousElementSibling.src" not in SOURCE


def test_web_build_uses_root_relative_assets_for_clean_routes():
    assert 'return f"/img/{name}.webp"' in BUILD_SOURCE
    assert 'url(/fonts/{name}.woff2)' in BUILD_SOURCE
    assert '<link rel="icon" href="{asset_root}favicon.png"' in BUILD_SOURCE


def test_admin_catalog_surfaces_incomplete_food_labelling():
    assert 'id="stat-incomplete"' in ADMIN_CATALOG
    assert 'id="catalog-filter"' in ADMIN_CATALOG
    assert "function completionState(item)" in ADMIN_CATALOG_JS
    assert "function missingPublicationFields(item)" in ADMIN_CATALOG_JS
    assert "Карточку можно опубликовать и дополнить позже" in ADMIN_CATALOG_JS
    assert "Карточку можно сохранить; важное ещё не заполнено" in ADMIN_CATALOG_JS
    assert "label: 'Нужно дополнить'" in ADMIN_CATALOG_JS
    assert "не мешают публикации" in ADMIN_CATALOG
    assert "publishingNow&&missing.length" not in ADMIN_CATALOG_JS
    assert "visibleText(value)" in ADMIN_CATALOG_JS
    assert "\\u3164" in ADMIN_CATALOG_JS


def test_catalog_supports_teaware_sections_and_multiple_product_photos():
    for category in (
        "teaware-teapots", "teaware-gaiwans", "teaware-cups", "teaware-chahai",
        "teaware-chahe", "teaware-figurines", "teaware-tools", "teaware-sets",
    ):
        assert category in SOURCE
    assert "Посуда" in ADMIN_CATALOG
    assert 'id="ts-gallery"' in SOURCE
    assert "image_urls" in SOURCE
    assert "file.multiple = true" in ADMIN_CATALOG_JS
    assert "/images/${index}/primary" in ADMIN_CATALOG_JS
    assert "data-add-group=\"teaware\"" in ADMIN_CATALOG
    assert "Форма переключена на посуду" in ADMIN_CATALOG_JS
    assert "Материал и техника изготовления" in ADMIN_CATALOG_JS
    assert "Вкусовой профиль" in ADMIN_CATALOG_JS
    assert "itemGroup(draft) === 'tea'" in ADMIN_CATALOG_JS
    assert "const teaware = typeGroup(m.t) === 'teaware'" in SOURCE
    assert "teaware_info_h:'О посуде'" in SOURCE
    assert "teaware_material:'Материал и техника'" in SOURCE
    assert "foodTitle.dataset.i18n = infoTitleKey" in SOURCE


def test_admin_catalog_has_owner_safe_creation_filters_preview_and_photo_queue():
    assert 'id="group-filter"' in ADMIN_CATALOG
    assert 'id="kind-dialog"' in ADMIN_CATALOG
    assert 'id="preview-dialog"' in ADMIN_CATALOG
    assert "img: 'logo-mark'" in ADMIN_CATALOG_JS
    assert "photo-pending__row" in ADMIN_CATALOG_JS
    assert "Порядок фотографий сохранён" in ADMIN_CATALOG_JS
    assert "Карточка сохранена, загружено фото" in ADMIN_CATALOG_JS
    assert "Технические настройки ссылки" in ADMIN_CATALOG_JS
    assert "catalog-row__open" in ADMIN_CATALOG_JS
    assert ".catalog-row{grid-template-columns:minmax(0,1fr) auto;padding:0;gap:0}" in ADMIN_CATALOG
    assert "catalog-lang-tab-${language}" in ADMIN_CATALOG_JS
    assert "catalog-lang-panel-${language}" in ADMIN_CATALOG_JS
    assert "['ArrowLeft', 'ArrowRight', 'Home', 'End']" in ADMIN_CATALOG_JS
    assert "function showSabyKindDialog(item)" in ADMIN_CATALOG_JS
    assert "const next = blankItem(group)" in ADMIN_CATALOG_JS
    assert ".photo input{position:absolute;inset:0;width:100%;height:100%;opacity:0;cursor:pointer}" in ADMIN_CATALOG
    assert "label.setAttribute('role', 'button')" not in ADMIN_CATALOG_JS


def test_admin_button_like_links_share_the_button_alignment_contract():
    assert "display:inline-flex;align-items:center;justify-content:center;gap:7px;line-height:1.2;text-align:center;text-decoration:none" in ADMIN_CATALOG
    assert '<a class="admin-nav__link" href="/manage">Обзор</a>' in ADMIN_CATALOG
    assert '<a class="admin-nav__link" href="/manage/catalog" aria-current="page">Каталог</a>' in ADMIN_CATALOG
    assert '<a class="btn" href="/shop" target="_blank" rel="noopener">Открыть магазин ↗</a>' in ADMIN_CATALOG
    assert "display:inline-flex;align-items:center;justify-content:center;line-height:1.2;text-align:center;text-decoration:none" in ADMIN_SOURCE


def test_admin_secondary_controls_are_visually_centered():
    assert ".move{width:30px;height:28px;border:1px solid var(--line);background:transparent;color:var(--muted);display:grid;place-items:center;padding:0;line-height:1}" in ADMIN_CATALOG
    assert ".dialog-close{width:42px;height:42px;border:1px solid var(--line2);background:transparent;color:var(--text);font-size:22px;display:grid;place-items:center;padding:0;line-height:1}" in ADMIN_CATALOG
    assert ".period__button{height:35px;min-width:54px;border-right:0;color:var(--muted);font-size:12px;display:inline-flex;align-items:center;justify-content:center;line-height:1.2;text-align:center}" in ADMIN_SOURCE
    assert ".owner-tools__actions .btn:first-child,.owner-tools__actions .btn:last-child{grid-column:1/-1}" in ADMIN_CATALOG


def test_admin_catalog_mobile_summary_wraps_without_a_clipped_horizontal_strip():
    assert ".stats{display:flex;overflow-x:auto;scroll-snap-type:x proximity" in ADMIN_CATALOG
    assert ".stats .stat{min-width:132px" in ADMIN_CATALOG
    assert "@media(max-width:900px){.items{max-height:none;overflow:visible}" in ADMIN_CATALOG


def test_admin_catalog_prioritizes_daily_work_and_keeps_optional_fields_non_blocking():
    assert ADMIN_CATALOG.index('class="layout"') < ADMIN_CATALOG.index('aria-label="Сервисные инструменты"')
    assert 'data-stat-filter="incomplete"' in ADMIN_CATALOG
    assert "optional.className = 'optional-fields'" in ADMIN_CATALOG_JS
    assert "Публикация не заблокирована" in ADMIN_CATALOG_JS
    assert "form.requestSubmit()" in ADMIN_CATALOG_JS


def test_catalog_escapes_owner_controlled_copy_before_using_inner_html():
    assert "const escHtml = value =>" in SOURCE
    assert '<${headingTag} class="tea__name">${escHtml(txt.n)}</${headingTag}>' in SOURCE
    assert '<p class="tea__orig">${escHtml(txt.o)}</p>' in SOURCE
    assert '<div class="citem__name">${escHtml(T().teas[it.id].n)}</div>' in SOURCE
    assert 'aria-label="${escAttr(T().cart_decrease' in SOURCE


def test_saby_review_distinguishes_unknown_stock_from_out_of_stock():
    assert "item.suggested_stock === null ? 'остаток не определён'" in ADMIN_CATALOG_JS
    assert "товар отмечен как недоступный" in ADMIN_CATALOG_JS


def test_mobile_legal_header_has_accessible_touch_targets():
    assert ".brand{min-width:44px;min-height:44px}" in LEGAL_CSS
    assert ".back{display:inline-flex;align-items:center;min-height:44px}" in LEGAL_CSS
    assert ".langs a{display:inline-flex;align-items:center;justify-content:center;min-width:44px;min-height:44px}" in LEGAL_CSS


def test_admin_guides_explain_stock_reservation_states():
    for label in (
        "Товар зарезервирован",
        "Резерв до списания Saby",
        "Остаток Saby обновлён",
        "Резерв истёк",
        "Резерв освобождён",
        "Резерв: нужна проверка",
    ):
        assert label in ADMIN_GUIDES
    assert "покупателю нужно начать оформление заново" in ADMIN_GUIDES


def test_admin_uses_route_neutral_copy_for_blocked_saby_operations():
    assert "Saby: отправка заблокирована" in ADMIN_SOURCE
    assert "Saby Delivery не подключён" not in ADMIN_SOURCE
