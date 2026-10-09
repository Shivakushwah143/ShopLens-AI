import pytest

from app.agent.deals_tracker import scraper
from app.agent.deals_tracker.scraper import FetchResult, ScrapedProduct, SourceSearchResult, extract_product_data, is_valid_product_link, search_retailer
from app.agent.deals_tracker.service import DealsService, parse_search_intent, _budget_refinement_query, _comparison_key


class DummySession:
    pass


class DealsServiceStub(DealsService):
    def __init__(self):
        pass

    def _persist_and_evaluate(self, p):
        return {"product_id": "test", "name": p.name, "price": p.price, "rating": p.rating, "link": p.link, "source": p.source, "deal_status": "AVERAGE"}


def test_parse_intent_builds_clean_retailer_requirements():
    intent = parse_search_intent("Find the best 3 gaming laptops under Rs 60,000 from Amazon, Flipkart, and Croma. Compare prices.")
    assert intent.category == "laptop"
    assert intent.max_budget == 60000
    assert intent.result_count == 3
    assert intent.retailers == ["amazon", "flipkart", "croma"]
    assert intent.specifications == ["gaming"]
    assert intent.search_query == "gaming laptop"


def test_parse_intent_auto_routes_available_retailers_only():
    intent = parse_search_intent("Find best 3 gaming laptops under Rs 60,000")
    assert intent.retailers == ["amazon", "flipkart"]
    assert "croma" not in intent.retailers


def test_parse_intent_respects_explicit_blocked_and_unsupported_retailers():
    intent = parse_search_intent("Find gaming laptops under Rs 60,000 from Croma and Myntra")
    assert intent.retailers == ["croma", "myntra"]
    assert intent.search_query == "gaming laptop"


@pytest.mark.asyncio
async def test_collect_reports_explicit_unsupported_and_blocked_without_searching(monkeypatch):
    service = DealsServiceStub()
    intent = parse_search_intent("Find gaming laptops under Rs 60,000 from Croma and Myntra")

    async def fail_search(*args, **kwargs):
        raise AssertionError("blocked/unsupported retailers must not be searched")

    monkeypatch.setattr("app.agent.deals_tracker.service.search_retailer", fail_search)
    collected = await service._collect_search_results(intent)
    statuses = {source["retailer"]: source["status"] for source in collected["source_statuses"]}
    assert statuses == {"croma": "blocked", "myntra": "unsupported"}
    assert collected["products"] == []


@pytest.mark.asyncio
async def test_auto_routing_searches_amazon_and_flipkart(monkeypatch):
    searched = []
    service = DealsServiceStub()
    intent = parse_search_intent("Find gaming laptops under Rs 60,000")

    async def fake_search(retailer, query, timeout_seconds=12.0):
        searched.append((retailer, query))
        return SourceSearchResult(retailer, "no_results", [], diagnostics={})

    monkeypatch.setattr("app.agent.deals_tracker.service.search_retailer", fake_search)
    await service._collect_search_results(intent)
    assert searched == [("amazon", "gaming laptop"), ("flipkart", "gaming laptop"), ("amazon", "gaming laptop under 60000"), ("flipkart", "gaming laptop under 60000")]


def test_parse_intent_removes_instruction_words_but_keeps_product_terms():
    intent = parse_search_intent("Search across available shopping platforms for best 3 Lenovo LOQ gaming laptops under Rs 60,000 and list their specifications ratings recommend")
    assert intent.max_budget == 60000
    assert intent.category == "laptop"
    assert intent.specifications == ["gaming"]
    assert intent.search_query == "gaming laptop lenovo loq"
    assert not any(word in intent.keywords for word in ["search", "across", "available", "shopping", "platforms", "their", "specifications", "ratings", "recommend"])


@pytest.mark.asyncio
async def test_flipkart_unknown_page_is_failed_not_no_results(monkeypatch):
    async def unknown_page(*args, **kwargs):
        return FetchResult("success", "<html><body><main>Something changed</main></body></html>", method="playwright", http_status=200, final_url="https://www.flipkart.com/search?q=gaming+laptop")

    monkeypatch.setattr(scraper, "_fetch_page_html", unknown_page)
    result = await search_retailer("flipkart", "gaming laptop")
    assert result.status == "failed"
    assert result.diagnostics["page_classification"] == "unknown"
    assert result.diagnostics["candidate_product_containers"] == 0


def test_flipkart_data_id_card_extraction():
    html = """
    <div data-id="ABC123">
      <a href="/brand-gaming-laptop/p/itmabc123"><div class="KzDlHZ">Brand Gaming Laptop Ryzen 5 RTX 3050</div></a>
      <div class="Nx9bqj">Rs 55,990</div><span>4.2 out of 5</span>
    </div>
    """
    products = extract_product_data(html, base_url="https://www.flipkart.com/search?q=gaming+laptop")
    assert len(products) == 1
    assert products[0].link == "https://www.flipkart.com/brand-gaming-laptop/p/itmabc123"
    assert products[0].price == 55990


def test_flipkart_title_removes_ui_rating_contamination():
    html = """
    <div data-id="ABC123">
      <a href="/brand-gaming-laptop/p/itmabc123"><div class="KzDlHZ">Brand Gaming Laptop A15 Ryzen 7 RTX 3050 16GB RAM 512GB SSD Add to Compare 4.3 out of 5 1,234 Ratings & Reviews</div></a>
      <div class="Nx9bqj">Rs 55,990</div><span>4.3 out of 5</span>
    </div>
    """
    products = extract_product_data(html, base_url="https://www.flipkart.com/search?q=gaming+laptop")
    assert products[0].rating == 4.3
    assert "Add to Compare" not in products[0].name
    assert "Ratings" not in products[0].name


def test_comparison_key_requires_same_model_and_configuration():
    a = "ASUS TUF A15 FA506NC Ryzen 5 7535HS RTX 3050 16GB RAM 512GB SSD"
    same = "ASUS TUF Gaming A15 FA506NC Ryzen 5 7535HS GeForce RTX 3050 16GB RAM 512GB SSD"
    same_series_variant = "ASUS TUF A15 FA506NF Ryzen 5 7535HS RTX 3050 16GB RAM 512GB SSD"
    different = "Lenovo LOQ 15IRX9 Intel i5-12450HX RTX 3050 16GB RAM 512GB SSD"
    assert _comparison_key(a) == _comparison_key(same)
    assert _comparison_key(a) != _comparison_key(same_series_variant)
    assert _comparison_key(a) != _comparison_key(different)
    assert _comparison_key("ASUS TUF A15 gaming laptop") is None


def test_preliminary_results_are_deduped_by_retailer_product_id():
    service = DealsServiceStub()
    intent = parse_search_intent("Find best 3 gaming laptops under Rs 80,000 from Amazon")
    product = ScrapedProduct("ASUS TUF A15 FA506NC Ryzen 5 7535HS RTX 3050 16GB RAM 512GB SSD", 75990, None, "https://www.amazon.in/tuf/dp/B1?ref=a", "amazon.in")
    source_results = [
        SourceSearchResult("amazon", "success", [product], diagnostics={}),
        SourceSearchResult("amazon", "success", [ScrapedProduct(product.name, 74990, None, "https://www.amazon.in/tuf/dp/B1?ref=b", "amazon.in")], diagnostics={}),
    ]
    _, _, preliminary = service._merge_source_results(source_results, intent)
    assert len(preliminary) == 1




def test_budget_refinement_query_uses_numeric_caps():
    intent_60 = parse_search_intent("Find best 3 gaming laptops under Rs 60,000 from Amazon")
    intent_150 = parse_search_intent("Find best 3 gaming laptops under Rs 150,000 from Amazon")
    assert _budget_refinement_query(intent_60) == "gaming laptop under 60000"
    assert _budget_refinement_query(intent_150) == "gaming laptop under 150000"


def test_gaming_laptop_category_accepts_known_model_lines_without_literal_laptop():
    service = DealsServiceStub()
    intent = parse_search_intent("Find best 3 gaming laptops under Rs 150,000 from Amazon")
    products = [
        ScrapedProduct("HP Victus Ryzen 5 RTX 3050 16GB RAM 512GB SSD", 58990, None, "https://www.amazon.in/victus/dp/B1", "amazon.in"),
        ScrapedProduct("Lenovo LOQ Intel Core i5 RTX 4050 144Hz FHD", 84990, None, "https://www.amazon.in/loq/dp/B2", "amazon.in"),
        ScrapedProduct("ASUS TUF A15 Ryzen 7 RTX 3050 DDR5", 96990, None, "https://www.amazon.in/tuf/dp/B3", "amazon.in"),
        ScrapedProduct("Acer Nitro V Intel Core i5 RTX 4050 16GB", 77990, None, "https://www.amazon.in/nitro/dp/B4", "amazon.in"),
        ScrapedProduct("Gaming mouse with RGB", 999, None, "https://www.amazon.in/mouse/dp/B5", "amazon.in"),
    ]
    verified, diagnostics = service._filter_verified_with_diagnostics(products, intent, "amazon")
    assert [p.name.split()[0] for p in verified] == ["HP", "Lenovo", "ASUS", "Acer"]
    assert diagnostics["rejected_by_category_or_spec"] == 1


def test_amazon_badge_text_is_removed_from_extracted_title():
    html = """
    <div data-component-type="s-search-result">
      <span>Amazon's Choice for "gaming laptop under 60000"</span>
      <h2><a href="/HP-Smartchoice-Ryzen-7320U/dp/B0HP15"><span class="a-size-medium a-color-base a-text-normal">HP 15 Smartchoice Ryzen 3 7320U 8GB RAM 512GB SSD</span></a></h2>
      <span class="a-price"><span>Rs 47,990</span></span>
    </div>
    """
    products = extract_product_data(html, base_url="https://www.amazon.in/s?k=gaming+laptop+under+60000")
    assert products[0].name == "HP 15 Smartchoice Ryzen 3 7320U 8GB RAM 512GB SSD"
    assert "gaming laptop under 60000" not in products[0].name.lower()


def test_generic_office_laptop_badged_as_gaming_is_rejected():
    service = DealsServiceStub()
    intent = parse_search_intent("Find best 3 gaming laptops under Rs 60,000 from Amazon")
    products = [
        ScrapedProduct("Amazon's Choice for gaming laptop under 60000 HP 15 Smartchoice Ryzen 3 7320U 8GB RAM 512GB SSD", 47990, None, "https://www.amazon.in/hp15/dp/B1", "amazon.in"),
        ScrapedProduct("HP Victus Ryzen 5 RTX 3050 16GB RAM 512GB SSD", 58990, None, "https://www.amazon.in/victus/dp/B2", "amazon.in"),
    ]
    verified, diagnostics = service._filter_verified_with_diagnostics(products, intent, "amazon")
    assert [p.name for p in verified] == ["HP Victus Ryzen 5 RTX 3050 16GB RAM 512GB SSD"]
    assert diagnostics["rejected_by_category_or_spec"] == 1


def test_rejection_samples_include_reason_name_and_price_only():
    service = DealsServiceStub()
    intent = parse_search_intent("Find best 3 gaming laptops under Rs 60,000 from Amazon")
    products = [
        ScrapedProduct("Lenovo LOQ Intel Core i5 RTX 3050 16GB RAM 512GB SSD", 119900, None, "https://www.amazon.in/loq/dp/B1", "amazon.in"),
        ScrapedProduct("Gaming mouse with RGB", 999, None, "https://www.amazon.in/mouse/dp/B2", "amazon.in"),
    ]
    _, diagnostics = service._filter_verified_with_diagnostics(products, intent, "amazon")
    assert {sample["reason"] for sample in diagnostics["rejection_samples"]} == {"budget", "category_or_spec"}
    assert all(set(sample) == {"name", "price", "reason"} for sample in diagnostics["rejection_samples"])

def test_extract_product_data_rejects_category_links_and_requires_price():
    html = """
    <div data-component-type="s-search-result">
      <h2><a href="/Gaming-Laptop-Brand/dp/B0ABC123"><span>Brand Gaming Laptop 16GB RTX 3050</span></a></h2>
      <span class="a-price"><span>Rs 59,990</span></span><span>4.3 out of 5</span>
    </div>
    <div data-component-type="s-search-result"><h2><a href="/s?k=gaming+laptop"><span>Shop by category</span></a></h2><span>Rs 60,000</span></div>
    <div data-component-type="s-search-result"><h2><a href="/Laptop-Sleeve/dp/B0SLEEVE"><span>Laptop sleeve cover</span></a></h2></div>
    """
    products = extract_product_data(html, base_url="https://www.amazon.in/s?k=gaming+laptop")
    assert len(products) == 1
    assert products[0].name == "Brand Gaming Laptop 16GB RTX 3050"
    assert products[0].price == 59990
    assert products[0].rating == 4.3
    assert is_valid_product_link(products[0].link)


def test_flipkart_product_card_extraction():
    html = """
    <div class="tUxRFH">
      <a href="/brand-gaming-laptop/p/itmabc123"><div class="KzDlHZ">Brand Gaming Laptop Ryzen 5</div></a>
      <div class="Nx9bqj">Rs 55,990</div><span>4.2 out of 5</span>
    </div>
    """
    products = extract_product_data(html, base_url="https://www.flipkart.com/search?q=gaming+laptop")
    assert len(products) == 1
    assert products[0].link == "https://www.flipkart.com/brand-gaming-laptop/p/itmabc123"
    assert products[0].price == 55990


def test_service_filters_budget_domain_category_specs_and_accessories():
    service = DealsServiceStub()
    intent = parse_search_intent("Find best 3 gaming laptops under Rs 60,000 from Amazon")
    products = [
        ScrapedProduct("Brand Gaming Laptop 16GB RTX 3050", 59990, None, "https://www.amazon.in/x/dp/B1", "amazon.in"),
        ScrapedProduct("Brand Gaming Laptop RTX 4050", 117990, None, "https://www.amazon.in/y/dp/B2", "amazon.in"),
        ScrapedProduct("Laptop sleeve cover", 999, None, "https://www.amazon.in/z/dp/B3", "amazon.in"),
        ScrapedProduct("Brand Gaming Laptop", 55990, None, "https://www.flipkart.com/item/p/itm1", "flipkart.com"),
    ]
    assert [p.name for p in service._filter_verified(products, intent, "amazon")] == ["Brand Gaming Laptop 16GB RTX 3050"]



def test_gaming_laptop_filter_accepts_gpu_signal_without_literal_gaming():
    service = DealsServiceStub()
    intent = parse_search_intent("Find best 3 gaming laptops under Rs 60,000 from Amazon")
    products = [
        ScrapedProduct("Lenovo LOQ Intel Core i5 RTX 3050 Laptop", 58990, None, "https://www.amazon.in/loq/dp/B1", "amazon.in"),
    ]
    verified, diagnostics = service._filter_verified_with_diagnostics(products, intent, "amazon")
    assert len(verified) == 1
    assert diagnostics["rejected_by_category_or_spec"] == 0
    assert diagnostics["rejected_by_budget"] == 0



def test_filter_only_zero_results_reports_no_results_source_status():
    service = DealsServiceStub()
    intent = parse_search_intent("Find best 3 gaming laptops under Rs 60,000 from Amazon")
    products = [
        ScrapedProduct("Lenovo LOQ Intel Core i5 RTX 3050 16GB RAM 512GB SSD", 119900, None, "https://www.amazon.in/loq/dp/B1", "amazon.in"),
    ]
    verified, diagnostics = service._filter_verified_with_diagnostics(products, intent, "amazon")
    status = "success" if verified else ("no_results" if "success" == "success" else "success")
    assert diagnostics["rejected_by_category_or_spec"] == 0
    assert diagnostics["rejected_by_budget"] == 1
    assert status == "no_results"


@pytest.mark.asyncio
async def test_source_timeout(monkeypatch):
    async def slow(*args, **kwargs):
        import asyncio
        await asyncio.sleep(0.05)
        return []
    monkeypatch.setattr(scraper, "_scrape_search_page_detailed", slow)
    result = await search_retailer("amazon", "gaming laptop", timeout_seconds=0.01)
    assert result.status == "timeout"


@pytest.mark.asyncio
async def test_http_403_is_blocked(monkeypatch):
    async def blocked(*args, **kwargs):
        return FetchResult("blocked", "", "HTTP 403 access restricted")
    monkeypatch.setattr(scraper, "_fetch_page_html", blocked)
    result = await search_retailer("croma", "gaming laptop")
    assert result.status == "blocked"
    assert "403" in (result.message or "")


def test_empty_results_status_is_no_results():
    service = DealsServiceStub()
    intent = parse_search_intent("gaming laptops under Rs 60000 from Amazon")
    result = service._finalize_search_results(intent, [], [{"retailer": "amazon", "status": "no_results", "count": 0}])
    assert result["status"] == "no_results"


def test_partial_success_status():
    service = DealsServiceStub()
    intent = parse_search_intent("gaming laptops under Rs 60000 from Amazon and Croma")
    products = [ScrapedProduct("Brand Gaming Laptop", 50000, None, "https://www.amazon.in/x/dp/B1", "amazon.in")]
    result = service._finalize_search_results(intent, products, [{"retailer": "amazon", "status": "success", "count": 1}, {"retailer": "croma", "status": "blocked", "count": 0}])
    assert result["status"] == "partial_success"


@pytest.mark.asyncio
async def test_sse_completion(monkeypatch):
    service = DealsServiceStub()
    async def collected(intent):
        return {"products": [], "source_statuses": [{"retailer": "amazon", "status": "no_results", "count": 0}], "preliminary_results": []}
    monkeypatch.setattr(service, "_collect_search_results", collected)
    events = [event async for event in service.stream_search(query="gaming laptops under Rs 60000 from Amazon")]
    assert events[-1]["event"] == "final"
    assert events[-1]["payload"]["status"] == "no_results"


def test_budget_parses_rupee_formats_and_excludes_keywords():
    cases = [
        ("gaming laptops under â‚¹60,000 from Amazon", 60000),
        ("gaming laptops under â‚¹150,000 from Amazon", 150000),
        ("gaming laptops under â‚¹1,50,000 from Amazon", 150000),
        ("gaming laptops under Rs. 60000 from Amazon", 60000),
        ("gaming laptops under INR 150000 from Amazon", 150000),
        ("gaming laptops under 1.5 lakh from Amazon", 150000),
    ]
    for query, expected in cases:
        intent = parse_search_intent(query)
        assert intent.max_budget == expected
        assert intent.search_query == "gaming laptop"
        assert not any(token in intent.keywords for token in ["60", "000", "150", "1", "5"])


def test_budget_is_hard_constraint_and_missing_rating_stays_null():
    service = DealsServiceStub()
    intent = parse_search_intent("Find best 3 gaming laptops under â‚¹150,000 from Amazon")
    products = [
        ScrapedProduct("Lenovo LOQ RTX 3050 16GB RAM 512GB SSD", 149999, None, "https://www.amazon.in/a/dp/B1", "amazon.in"),
        ScrapedProduct("Alienware RTX 5060 16GB RAM 512GB SSD", 150001, 4.5, "https://www.amazon.in/b/dp/B2", "amazon.in"),
    ]
    verified = service._filter_verified(products, intent, "amazon")
    assert len(verified) == 1
    assert verified[0].rating is None
    assert verified[0].price <= intent.max_budget


def test_top_three_limit_and_deduped_ranking():
    service = DealsServiceStub()
    intent = parse_search_intent("Find best 3 gaming laptops under â‚¹150,000 from Amazon")
    products = [
        ScrapedProduct("A RTX Gaming Laptop 16GB RAM SSD", 90000, None, "https://www.amazon.in/a/dp/B1", "amazon.in"),
        ScrapedProduct("A RTX Gaming Laptop 16GB RAM SSD", 88000, None, "https://www.amazon.in/a/dp/B1?ref=x", "amazon.in"),
        ScrapedProduct("B RTX Gaming Laptop 16GB RAM SSD", 100000, None, "https://www.amazon.in/b/dp/B2", "amazon.in"),
        ScrapedProduct("C RTX Gaming Laptop 16GB RAM SSD", 110000, None, "https://www.amazon.in/c/dp/B3", "amazon.in"),
        ScrapedProduct("D RTX Gaming Laptop 16GB RAM SSD", 120000, None, "https://www.amazon.in/d/dp/B4", "amazon.in"),
    ]
    final = service._finalize_search_results(intent, products, [{"retailer": "amazon", "status": "success", "count": 4}])
    assert len(final["results"]) == 3
    assert [r["price"] for r in final["results"]] == [88000, 100000, 110000]


def test_zero_match_handling_after_budget_filter():
    service = DealsServiceStub()
    intent = parse_search_intent("Find best 3 gaming laptops under â‚¹60,000 from Amazon")
    products = [ScrapedProduct("A RTX Gaming Laptop 16GB RAM SSD", 90000, None, "https://www.amazon.in/a/dp/B1", "amazon.in")]
    verified, diagnostics = service._filter_verified_with_diagnostics(products, intent, "amazon")
    final = service._finalize_search_results(intent, verified, [{"retailer": "amazon", "status": "no_results", "count": 0, "diagnostics": diagnostics}])
    assert verified == []
    assert diagnostics["rejected_by_budget"] == 1
    assert final["status"] == "no_results"


def test_frontend_uses_inr_intl_formatter():
    from pathlib import Path
    js = Path("app/ui/deals.js").read_text(encoding="utf-8")
    assert "Intl.NumberFormat" in js
    assert "currency: \"INR\"" in js
    assert "ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¹" not in js[js.index("function formatPrice"):js.index("function badgeClass")]

