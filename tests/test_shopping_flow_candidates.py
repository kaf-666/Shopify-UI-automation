from types import SimpleNamespace

from flows.shopping_flow import ShoppingFlow


def test_second_candidate_is_opened_from_the_original_collection(monkeypatch):
    original = "https://shop.example/collections/dresses?sort_by=price"
    page = SimpleNamespace(url=original)
    navigations = []

    def goto(url, **_kwargs):
        navigations.append(url)
        page.url = url

    page.goto = goto

    class Collection:
        def open_product(self, index):
            assert page.url == original
            page.url = f"https://shop.example/products/{index}"

    flow = ShoppingFlow(page, {"base_url": "https://shop.example"}, "desktop")
    flow.state.update(coll=Collection(), product_count=2)
    monkeypatch.setattr("flows.shopping_flow.ProductPage", lambda page, *_args: SimpleNamespace(url=page.url))
    monkeypatch.setattr(flow, "_suitable", lambda product: product.url.endswith("/1"))

    index, selected = flow.open_product()
    assert index == 1 and selected.url.endswith("/1")
    assert navigations == [original]
