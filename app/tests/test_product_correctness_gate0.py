from app.agent.deals_tracker.scraper import ScrapedProduct
from app.agent.deals_tracker.service import DealsService, parse_search_intent


class DealsServiceStub(DealsService):
    def __init__(self):
        pass


def _product(name, price=999, link_id="B0TEST", attrs=None):
    return ScrapedProduct(
        name=name,
        price=price,
        rating=None,
        link=f"https://www.amazon.in/item/dp/{link_id}",
        source="amazon.in",
        attributes=attrs,
    )


def _verified_names(query, products):
    service = DealsServiceStub()
    intent = parse_search_intent(query)
    verified, _ = service._filter_verified_with_diagnostics(products, intent, "amazon")
    return [p.name for p in verified]


def test_gate0_real_world_smart_phone_query_extracts_budget():
    intent = parse_search_intent("a smart phone with 10000 price")
    assert intent.max_budget == 10000


def test_gate0_smartphone_rejects_accessories():
    products = [
        _product("Samsung Galaxy M14 5G Smartphone 4GB RAM 128GB Storage", 9999, "B0PHONE"),
        _product("Cute smart phone charm accessory", 199, "B0CHARM"),
        _product("Smart phone case cover", 299, "B0CASE"),
        _product("Fast phone charger Type C", 599, "B0CHARGER"),
        _product("Mobile phone cleaning kit", 149, "B0CLEAN"),
    ]
    assert _verified_names("a smart phone with 10000 price", products) == [
        "Samsung Galaxy M14 5G Smartphone 4GB RAM 128GB Storage"
    ]


def test_gate0_gaming_laptop_rejects_bags_and_accessories():
    products = [
        _product("Lenovo LOQ Intel Core i5 RTX 3050 Laptop 16GB RAM 512GB SSD", 58990, "B0LOQ"),
        _product("Gaming laptop bag for 15.6 inch laptops", 1499, "B0BAG"),
        _product("Universal laptop charger adapter", 1299, "B0ADAPTER"),
        _product("Gaming laptop cooling pad stand", 999, "B0COOL"),
    ]
    assert _verified_names("gaming laptop under Rs 60000", products) == [
        "Lenovo LOQ Intel Core i5 RTX 3050 Laptop 16GB RAM 512GB SSD"
    ]


def test_gate0_cotton_tshirt_rejects_unstitched_fabric_and_wrong_material():
    products = [
        _product("Men Cotton T-Shirt Regular Fit", 499, "B0TEE"),
        _product("Cotton unstitched fabric material for men", 399, "B0FABRIC"),
        _product("Men Polyester T-Shirt Regular Fit", 349, "B0POLY"),
    ]
    assert _verified_names("men cotton t-shirt under Rs 1000", products) == [
        "Men Cotton T-Shirt Regular Fit"
    ]


def test_gate0_oversized_100_percent_cotton_requires_confirmed_fit_and_fabric():
    products = [
        _product("Men Oversized 100% Cotton T-Shirt", 599, "B0OS"),
        _product("Men 100% Cotton T-Shirt", 499, "B0NOFIT"),
        _product("Men Oversized Cotton Blend T-Shirt", 399, "B0BLEND"),
        _product("Men Plain T-Shirt", 549, "B0ATTRS", {"fit": "oversized", "fabric": "100% cotton"}),
    ]
    assert _verified_names("men oversized 100% cotton t-shirt under Rs 1000", products) == [
        "Men Oversized 100% Cotton T-Shirt",
        "Men Plain T-Shirt",
    ]


def test_gate0_budget_is_enforced_before_recommendation():
    products = [
        _product("Acer Nitro V Intel Core i5 RTX 3050 Laptop", 59990, "B0NITRO"),
        _product("ASUS TUF A15 Ryzen 7 RTX 4050 Laptop", 79990, "B0TUF"),
    ]
    assert _verified_names("gaming laptop under Rs 60000", products) == [
        "Acer Nitro V Intel Core i5 RTX 3050 Laptop"
    ]
