"""Unit tests for the website observation layer."""

import unittest

from src.crawler.crawler import FetchedPage
from src.observation.observation import (
    ImageObservation,
    JsonLdObservation,
    LinkObservation,
    PageObservation,
    PageSignals,
    TableObservation,
    create_page_observation,
    observe_html,
    observe_page,
)


class TestObservation(unittest.TestCase):
    """Test suite for observe_page and PageObservation extraction logic."""

    def test_basic_metadata_extraction(self) -> None:
        """Verify extraction of title, meta description, and canonical link."""
        html = """
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <title>   Nike Air Zoom Pegasus 40 - Official Store   </title>
            <meta name="description" content="Shop the Pegasus 40 running shoe with responsive cushioning.">
            <link rel="canonical" href="/products/pegasus-40">
        </head>
        <body>
            <p>Welcome</p>
        </body>
        </html>
        """
        page = FetchedPage(
            requested_url="https://nike.example.com/products/pegasus-40?ref=ad",
            final_url="https://nike.example.com/products/pegasus-40",
            status_code=200,
            content_type="text/html",
            html_content=html,
            is_success=True,
        )

        obs: PageObservation = observe_page(page)

        self.assertTrue(obs.is_success)
        self.assertEqual(obs.title, "Nike Air Zoom Pegasus 40 - Official Store")
        self.assertEqual(
            obs.meta_description,
            "Shop the Pegasus 40 running shoe with responsive cushioning.",
        )
        self.assertEqual(obs.canonical_url, "https://nike.example.com/products/pegasus-40")
        self.assertTrue(obs.has_title)
        self.assertTrue(obs.has_meta_description)
        self.assertTrue(obs.has_canonical)

    def test_heading_extraction(self) -> None:
        """Verify extraction of h1, h2, and h3 headings with normalized whitespace."""
        html = """
        <html>
        <body>
            <h1>  Running   Shoes  </h1>
            <h2>Men's Collection</h2>
            <h2>Women's Collection</h2>
            <h3> Pegasus   Series </h3>
            <h3>Infinity Run</h3>
            <h3>Invincible</h3>
        </body>
        </html>
        """
        obs = observe_html(html)

        self.assertEqual(obs.h1, ["Running Shoes"])
        self.assertEqual(obs.h2, ["Men's Collection", "Women's Collection"])
        self.assertEqual(obs.h3, ["Pegasus Series", "Infinity Run", "Invincible"])
        self.assertEqual(obs.num_headings, 6)
        self.assertEqual(obs.signals.num_headings, 6)

    def test_visible_text_extraction(self) -> None:
        """Verify script/style/noscript/template content is omitted from visible text."""
        html = """
        <html>
        <head>
            <style>
                .price { color: red; font-size: 20px; }
                body { background: #fff; }
            </style>
            <script>
                var secretApiKey = "xyz123";
                function track() { return true; }
            </script>
        </head>
        <body>
            <h1>Product Details</h1>
            <noscript>You need JavaScript to view full specs.</noscript>
            <template>
                <div>Template hidden blueprint</div>
            </template>
            <p>
                Price:    ₹12,999
            </p>
            <p>Available in all sizes. Fast free delivery.</p>
        </body>
        </html>
        """
        obs = observe_html(html)

        # Visible text checks
        self.assertIn("Product Details", obs.visible_text)
        self.assertIn("Price: ₹12,999", obs.visible_text)
        self.assertIn("Available in all sizes. Fast free delivery.", obs.visible_text)

        # Must NOT include non-visible tag content
        self.assertNotIn("secretApiKey", obs.visible_text)
        self.assertNotIn("font-size", obs.visible_text)
        self.assertNotIn("You need JavaScript", obs.visible_text)
        self.assertNotIn("Template hidden blueprint", obs.visible_text)
        self.assertNotIn("<p>", obs.visible_text)

    def test_link_extraction_and_classification(self) -> None:
        """Verify link resolution, anchor text extraction, and internal/external classification."""
        html = """
        <html>
        <body>
            <a href="/cart">View Cart</a>
            <a href="https://example.com/checkout">Proceed to Checkout</a>
            <a href="https://external-partner.com/track">Partner Tracking</a>
            <a href="mailto:support@example.com">Email Us</a>
            <a href="#specifications">Jump to Specs</a>
            <a href="">Empty Link</a>
        </body>
        </html>
        """
        page = FetchedPage(
            requested_url="http://example.com/shop",
            final_url="https://example.com/shop",
            status_code=200,
            content_type="text/html",
            html_content=html,
            is_success=True,
        )

        obs = observe_page(page)

        self.assertEqual(len(obs.links), 6)
        self.assertEqual(obs.num_links, 6)

        # 1. Relative link resolved to base
        link_cart = obs.links[0]
        self.assertEqual(link_cart.raw_href, "/cart")
        self.assertEqual(link_cart.href, "https://example.com/cart")
        self.assertEqual(link_cart.anchor_text, "View Cart")
        self.assertTrue(link_cart.is_internal)

        # 2. Absolute same-domain link
        link_checkout = obs.links[1]
        self.assertEqual(link_checkout.href, "https://example.com/checkout")
        self.assertTrue(link_checkout.is_internal)

        # 3. External link
        link_external = obs.links[2]
        self.assertEqual(link_external.href, "https://external-partner.com/track")
        self.assertEqual(link_external.anchor_text, "Partner Tracking")
        self.assertFalse(link_external.is_internal)

        # 4. Mailto link (external protocol)
        link_mail = obs.links[3]
        self.assertEqual(link_mail.href, "mailto:support@example.com")
        self.assertFalse(link_mail.is_internal)

        # 5. Fragment anchor link on same page
        link_hash = obs.links[4]
        self.assertEqual(link_hash.href, "https://example.com/shop#specifications")
        self.assertTrue(link_hash.is_internal)

    def test_image_extraction(self) -> None:
        """Verify image source resolution and alt text extraction."""
        html = """
        <html>
        <body>
            <img src="/images/shoe-front.jpg" alt="Pegasus 40 Front View">
            <img src="https://cdn.example.com/shoe-sole.png" alt=" Durable rubber sole ">
            <img src="/images/no-alt.jpg">
        </body>
        </html>
        """
        obs = observe_html(html, final_url="https://example.com/products/shoe")

        self.assertEqual(len(obs.images), 3)
        self.assertEqual(obs.num_images, 3)

        self.assertEqual(obs.images[0].src, "https://example.com/images/shoe-front.jpg")
        self.assertEqual(obs.images[0].alt, "Pegasus 40 Front View")

        self.assertEqual(obs.images[1].src, "https://cdn.example.com/shoe-sole.png")
        self.assertEqual(obs.images[1].alt, "Durable rubber sole")

        self.assertEqual(obs.images[2].src, "https://example.com/images/no-alt.jpg")
        self.assertEqual(obs.images[2].alt, "")

    def test_table_extraction(self) -> None:
        """Verify structured extraction of table rows and headers."""
        html = """
        <html>
        <body>
            <table id="specs">
                <thead>
                    <tr><th>Feature</th><th>Specification</th></tr>
                </thead>
                <tbody>
                    <tr><td>Weight</td><td>285 grams</td></tr>
                    <tr><td>Drop</td><td>10 mm</td></tr>
                </tbody>
            </table>

            <table id="simple">
                <tr><th>Size</th><th>Stock</th></tr>
                <tr><td>UK 8</td><td>In Stock</td></tr>
                <tr><td>UK 9</td><td>Out of Stock</td></tr>
            </table>
        </body>
        </html>
        """
        obs = observe_html(html)

        self.assertEqual(len(obs.tables), 2)
        self.assertEqual(obs.num_tables, 2)

        # Table 1: with explicit thead
        t1 = obs.tables[0]
        self.assertEqual(t1.headers, ["Feature", "Specification"])
        self.assertEqual(len(t1.rows), 3)  # header row + 2 data rows
        self.assertEqual(t1.rows[1], ["Weight", "285 grams"])
        self.assertEqual(t1.rows[2], ["Drop", "10 mm"])

        # Table 2: without thead, but first row has <th>
        t2 = obs.tables[1]
        self.assertEqual(t2.headers, ["Size", "Stock"])
        self.assertEqual(t2.rows[1], ["UK 8", "In Stock"])
        self.assertEqual(t2.rows[2], ["UK 9", "Out of Stock"])

    def test_json_ld_extraction(self) -> None:
        """Verify extraction of valid JSON objects, JSON arrays, and malformed blocks."""
        html = """
        <html>
        <head>
            <!-- 1. Valid single JSON-LD object -->
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Pegasus 40",
                "price": 12999
            }
            </script>

            <!-- 2. Valid JSON-LD array -->
            <script type="application/ld+json">
            [
                {
                    "@context": "https://schema.org",
                    "@type": "BreadcrumbList",
                    "itemListElement": []
                }
            ]
            </script>

            <!-- 3. Malformed JSON-LD -->
            <script type="application/ld+json">
                { "name": "Broken JSON", invalid_syntax: }
            </script>

            <!-- 4. Non-JSON-LD script should be ignored -->
            <script type="text/javascript">
                console.log("Not JSON-LD");
            </script>
        </head>
        </html>
        """
        obs = observe_html(html)

        self.assertEqual(len(obs.json_ld), 3)

        # 1. Single object
        j1 = obs.json_ld[0]
        self.assertTrue(j1.is_valid)
        self.assertIsInstance(j1.parsed_data, dict)
        self.assertEqual(j1.parsed_data.get("name"), "Pegasus 40")
        self.assertEqual(j1.parsed_data.get("price"), 12999)
        self.assertIsNone(j1.error_message)

        # 2. JSON array
        j2 = obs.json_ld[1]
        self.assertTrue(j2.is_valid)
        self.assertIsInstance(j2.parsed_data, list)
        self.assertEqual(len(j2.parsed_data), 1)
        self.assertEqual(j2.parsed_data[0].get("@type"), "BreadcrumbList")

        # 3. Malformed JSON block
        j3 = obs.json_ld[2]
        self.assertFalse(j3.is_valid)
        self.assertIsNone(j3.parsed_data)
        self.assertIn("Broken JSON", j3.raw_text)
        self.assertIsNotNone(j3.error_message)

    def test_failed_fetched_page_handling(self) -> None:
        """Verify failed FetchedPage is handled gracefully without crashing."""
        failed_page = FetchedPage(
            requested_url="https://example.com/missing",
            final_url="https://example.com/missing",
            status_code=404,
            content_type="text/html",
            html_content="",
            is_success=False,
            error_message="HTTP error status 404 (Not Found).",
        )

        obs = observe_page(failed_page)

        self.assertFalse(obs.is_success)
        self.assertEqual(obs.status_code, 404)
        self.assertEqual(obs.requested_url, "https://example.com/missing")
        self.assertIn("404", obs.error_message or "")
        self.assertIsNone(obs.title)
        self.assertIsNone(obs.meta_description)
        self.assertIsNone(obs.canonical_url)
        self.assertEqual(obs.h1, [])
        self.assertEqual(obs.visible_text, "")
        self.assertEqual(obs.links, [])
        self.assertEqual(obs.images, [])
        self.assertEqual(obs.tables, [])
        self.assertEqual(obs.json_ld, [])
        self.assertFalse(obs.has_title)
        self.assertEqual(obs.num_links, 0)

    def test_empty_and_minimal_html_handling(self) -> None:
        """Verify empty and minimal HTML pages parse cleanly without error."""
        # Minimal empty tags
        obs = observe_html("<html><head></head><body></body></html>")
        self.assertTrue(obs.is_success)
        self.assertIsNone(obs.title)
        self.assertIsNone(obs.meta_description)
        self.assertEqual(obs.visible_text, "")
        self.assertEqual(obs.num_links, 0)
        self.assertEqual(obs.num_images, 0)
        self.assertEqual(obs.num_tables, 0)
        self.assertEqual(obs.num_headings, 0)
        self.assertFalse(obs.has_title)

        # Empty body with only whitespaces
        obs_empty = observe_html("   \n   ")
        self.assertTrue(obs_empty.is_success)
        self.assertEqual(obs_empty.visible_text, "")

    def test_create_page_observation_alias(self) -> None:
        """Verify create_page_observation function alias works identically to observe_page."""
        page = FetchedPage(
            requested_url="https://example.com/",
            final_url="https://example.com/",
            status_code=200,
            content_type="text/html",
            html_content="<title>Alias Test</title><h1>Title</h1>",
            is_success=True,
        )
        obs = create_page_observation(page)
        self.assertEqual(obs.title, "Alias Test")
        self.assertEqual(obs.h1, ["Title"])


if __name__ == "__main__":
    unittest.main()
