"""Offline contracts for shared PDP-to-cart product identity matching."""

from flows.shopping_flow import product_match_basis


PDP_TITLE = "A-Line/Princess V-Neck Floor-Length Mother of the Bride (SHI22501284)"
CART_TITLE = "A-Line/Princess V-Neck Floor-Length Mother of the Bride"
PRODUCT_PATH = "/products/a-line-princess-v-neck-floor-length-mother-shi22501284"


def test_matching_product_path_accepts_display_title_without_sku_suffix() -> None:
    assert product_match_basis(
        PDP_TITLE,
        CART_TITLE,
        pdp_url=f"https://shop.example{PRODUCT_PATH}?variant=48809988194474",
        cart_identity={
            "product_path": PRODUCT_PATH,
            "product_url_variant_id": "48809988194474",
        },
    ) == "PRODUCT_PATH"


def test_matching_path_rejects_conflicting_variant_ids() -> None:
    assert product_match_basis(
        PDP_TITLE,
        CART_TITLE,
        pdp_url=f"https://shop.example{PRODUCT_PATH}?variant=111",
        cart_identity={"product_path": PRODUCT_PATH, "product_url_variant_id": "222"},
    ) is None


def test_different_product_paths_reject_even_matching_titles() -> None:
    assert product_match_basis(
        "Same Dress",
        "Same Dress",
        pdp_url="https://shop.example/products/first-dress",
        cart_identity={"product_path": "/products/second-dress"},
    ) is None


def test_title_contract_remains_fallback_when_path_identity_is_unavailable() -> None:
    assert product_match_basis("SKU DRESS", "DRESS") == "RENDERED_TITLE"
    assert product_match_basis(
        "Styled Dress", "DRESS", pdp_title_stored="SKU DRESS"
    ) == "STORED_TITLE"


def test_different_titles_without_product_paths_still_fail() -> None:
    assert product_match_basis("First Dress", "Second Dress") is None


def test_parenthetical_identifier_matches_only_when_product_handle_confirms_it() -> None:
    checkout_title = "A-Line/Princess V-Neck Floor-Length Mother of the Bride"
    assert product_match_basis(
        PDP_TITLE,
        checkout_title,
        pdp_url=f"https://shop.example{PRODUCT_PATH}",
    ) == "PATH_CONFIRMED_TITLE_SUFFIX"
    assert product_match_basis(
        PDP_TITLE,
        checkout_title,
        pdp_url="https://shop.example/products/a-line-princess-v-neck-floor-length-mother-dress-unknown",
    ) is None


def test_parenthetical_identifier_does_not_hide_a_different_checkout_title() -> None:
    assert product_match_basis(
        PDP_TITLE,
        "A-Line/Princess V-Neck Floor-Length Different Bride",
        pdp_url=f"https://shop.example{PRODUCT_PATH}",
    ) is None
