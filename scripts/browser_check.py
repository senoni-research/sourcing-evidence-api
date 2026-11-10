"""Drive the hosted review UI in Chromium and assert the reviewed behaviours.

Playwright is not part of the pinned environment; run this with an interpreter that has it:

    python scripts/browser_check.py --base-url http://127.0.0.1:8765 --shots /tmp/shots

Every run uses a fresh browser context, so it creates its own fictional reviews.
"""

import argparse
import os
import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

REASON = "Supplier clarification on page 4 says the proposal applies to revision A."


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--shots", default="")
    parser.add_argument("--chromium", default=os.getenv("CHROMIUM_PATH", ""))
    args = parser.parse_args()
    shots = Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=args.chromium or None)
        context = browser.new_context(viewport={"width": 1363, "height": 936})
        page = context.new_page()
        errors = []
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: errors.append(str(error)))

        def shot(name, full=False):
            if shots:
                page.screenshot(path=str(shots / f"{name}.png"), full_page=full)

        # 1. The blocked case opens with the disputed source line beside the issue, nothing prefilled.
        # The bare link opens the complete case, so the blocked case is addressed by its hash.
        page.goto(args.base_url + "/#scope_mismatch")
        expect(page.locator("#headline")).to_have_text("Review a supplier’s 12% price increase")
        expect(page.locator("#strip")).to_contain_text("£100.00 → £112.00")
        expect(page.locator("#strip")).to_contain_text("1 blocking issue · calculation withheld")
        expect(page.locator(".line.selected")).to_contain_text("proposal.revision = B")
        expect(page.locator(".line.flagged")).to_have_count(1)
        expect(page.locator("#issues")).to_contain_text("Proposal revision: source document states B")
        assert page.locator(".line.selected").bounding_box()["y"] < 936, "evidence is above the fold"
        assert page.locator("#issue-title").bounding_box()["y"] < 936, "issue is above the fold"
        expect(page.locator("#calculate")).to_have_count(0)
        expect(page.locator("#scenario button")).to_be_disabled()
        expect(page.locator("#scenario")).to_contain_text("Unavailable while the review is blocked")
        sizes = page.evaluate(
            """() => ['#issues p', '.line', '.strip dd', '#result p', '.muted', '.strip dt']
                .map(s => [s, parseFloat(getComputedStyle(document.querySelector(s)).fontSize)])"""
        )
        assert all(size >= 13 for _, size in sizes), sizes
        assert all(size >= 14 for name, size in sizes[:4]), sizes
        shot("01-blocked-evidence-beside-issue")

        # 2. A correction needs a deliberate value, basis, citation and reason.
        page.click("#start-correction")
        expect(page.locator("#correction-value")).to_have_value("")
        expect(page.locator("#correction-reason")).to_have_value("")
        expect(page.locator("input[name=basis]:checked")).to_have_count(0)
        page.click("#correction-form button[type=submit]")
        expect(page.locator("#correction-error")).to_contain_text("Enter the value you are asserting")
        expect(page.locator("#correction-error")).to_contain_text("Choose what supports the value")
        expect(page.locator("#correction-error")).to_be_focused()

        page.fill("#correction-value", "A")
        page.check("#basis-cited")
        page.fill("#correction-reason", REASON)
        # Citing the line that says B is refused by the service, with the form kept intact.
        page.click("#cite-selected")
        page.click("#correction-form button[type=submit]")
        expect(page.locator("#correction-error")).to_contain_text("does not state the value A")
        expect(page.locator("#correction-value")).to_have_value("A")
        page.click("#tab-4")
        expect(page.locator("#source-foot")).to_contain_text("parser does not read it")
        page.click("#line-9")
        page.click("#cite-selected")
        expect(page.locator(".cite-box")).to_contain_text("page 4, line 9")
        shot("02-correction-form-with-cited-passage")
        page.click("#correction-form button[type=submit]")

        # 3. Result, with source value and reviewer assertion still distinct.
        expect(page.locator("#override-title")).to_be_focused()
        expect(page.locator("#override")).to_contain_text("Source document states")
        expect(page.locator("#override")).to_contain_text("Reviewer asserts")
        expect(page.locator("#result")).to_contain_text("£102.30")
        expect(page.locator("#result")).to_contain_text("one reviewer override (cited passage)")
        expect(page.locator("#strip")).to_contain_text("reviewer uses A")
        expect(page.locator(".line.overridden")).to_contain_text("proposal.revision = B")
        expect(page.locator("#live")).to_contain_text("Override recorded")
        shot("03-resolved-with-override")

        # 4. What-if: draft state, stale marker, thresholds that follow the active inputs.
        expect(page.locator("#draft-state")).to_contain_text("equals the reviewed movements")
        page.fill("#move-materials", "10")
        expect(page.locator("#draft-state")).to_contain_text("Draft changed and not calculated yet")
        page.fill("#move-energy", "-20")
        page.fill("#move-labour", "0")
        page.click("#calculate")
        expect(page.locator("#comparison-title")).to_be_focused()
        expect(page.locator("#comparison tfoot")).to_contain_text("£101.50")
        expect(page.locator("#comparison tfoot")).to_contain_text("£102.30")
        materials = page.locator("#break-even tbody tr").first
        expect(materials).to_contain_text("+25.40%")
        expect(materials).to_contain_text("+31.00%")
        expect(page.locator("#draft-state")).to_contain_text("matches the what-if calculated")
        page.fill("#move-freight", "7")
        expect(page.locator("#stale-chip")).to_be_visible()
        expect(page.locator("#comparison")).to_have_class(re.compile("stale"))
        expect(page.locator("#draft-state")).to_contain_text("still shows the earlier inputs")
        shot("04-what-if-stale-draft", full=True)
        page.fill("#move-freight", "5")
        expect(page.locator("#stale-chip")).to_have_count(0)

        # 5. History in buyer language; hashes only in the engineering view.
        expect(page.locator("#events")).to_contain_text("Proposal revision: B (source) → A (reviewer)")
        expect(page.locator("#events")).to_contain_text("Hypothetical")
        assert not re.search(r"[0-9a-f]{32}", page.locator("#review-view").inner_text())
        page.click("#view-engineering")
        expect(page.locator("#engineering")).to_contain_text("Source document SHA-256")
        expect(page.locator("#engineering")).to_contain_text("what_if_calculated")
        expect(page.locator("#engineering")).to_contain_text("POST /demo/scope_mismatch/sensitivity → 200")
        expect(page.locator("#api-docs")).to_have_text("Interactive API docs")
        expect(page.locator("#api-docs")).to_have_attribute("href", "/docs")
        expect(page.locator("#view-source")).to_have_text("View source")
        expect(page.locator("#view-source")).to_have_attribute("rel", "noopener")
        expect(page.locator("#view-source")).to_have_attribute(
            "href", "https://github.com/senoni-research/sourcing-evidence-api"
        )
        assert "[object" not in page.locator("#main").inner_text()
        shot("05-engineering-view", full=True)
        # Swagger UI loads its script and stylesheet from a CDN; the page renders only if they load.
        docs = context.new_page()
        docs.goto(args.base_url + "/docs")
        expect(docs.locator(".swagger-ui .opblock")).to_have_count(9)
        expect(docs.locator(".swagger-ui")).to_contain_text("/demo/{case}/correction")
        docs.close()
        page.click("#view-review")

        # 6. Case switching keeps each review; reload resumes it.
        page.click("#cases button:has-text('Missing energy index')")
        expect(page.locator(".line.selected")).to_contain_text("driver.energy = ")
        expect(page.locator(".line.selected")).to_contain_text("current_index:UNKNOWN")
        expect(page.locator("#issues")).to_contain_text("no value (UNKNOWN)")
        shot("06-missing-energy-source")
        page.click("#cases button:has-text('Specification changed')")
        expect(page.locator("#override")).to_contain_text("Reviewer asserts")
        expect(page.locator("#comparison tfoot")).to_contain_text("£101.50")
        expect(page.locator("#cases")).to_contain_text("Resolved by reviewer override · what-if saved")
        page.reload()
        expect(page.locator("#override")).to_contain_text(REASON)
        expect(page.locator("#history-use")).to_contain_text("3 of 40 entries used")

        # 7. A stale tab gets a recovery path instead of overwriting.
        other = context.new_page()
        other.goto(args.base_url + "/#scope_mismatch")
        expect(other.locator("#override")).to_contain_text("Reviewer asserts")
        other.click("#discard")
        expect(other.locator("#live")).to_contain_text("Saved what-if discarded")
        page.fill("#move-materials", "12")
        page.click("#calculate")
        expect(page.locator("#alert")).to_contain_text("changed in another tab")
        expect(page.locator("#alert")).to_contain_text("version 4")
        expect(page.locator("#move-materials")).to_have_value("12")
        shot("07-stale-tab-recovery")
        page.click("#alert-dismiss")
        page.click("#calculate")
        expect(page.locator("#comparison-title")).to_be_focused()
        expect(page.locator("#history-use")).to_contain_text("5 of 40 entries used")
        other.close()

        # 8. Amend and revert stay available; the revert is recorded.
        page.click("#amend")
        expect(page.locator("#correction-value")).to_have_value("A")
        page.click("#correction-form button:has-text('Cancel')")
        page.click("#revert")
        page.fill("#revert-reason", "Returning to the source document for the walkthrough.")
        page.click("#revert-form button[type=submit]")
        expect(page.locator("#issue-title")).to_be_focused()
        expect(page.locator("#events")).to_contain_text(
            "Override reverted · Proposal revision: A (reviewer) → B (source)"
        )
        expect(page.locator("#events")).to_contain_text("saved what-if was discarded")
        expect(page.locator("#strip")).to_contain_text("calculation withheld")

        # 9. Narrow screens use source/decision tabs and do not overflow.
        for width in (390, 320):
            page.set_viewport_size({"width": width, "height": 800})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), width
            expect(page.locator("#source-pane")).to_be_hidden()
            page.click("#issues button:has-text('Show source')")
            expect(page.locator("#source-pane")).to_be_visible()
            expect(page.locator(".line.selected")).to_be_focused()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), width
            if width == 390:
                shot("08-mobile-source-pane")
            page.click("#pane-decision")
            if width == 390:
                shot("09-mobile-decision-pane", full=True)

        # The two refused requests above (422, 409) are deliberate; anything else is a defect.
        expected = ("status of 422", "status of 409")
        unexpected = [text for text in errors if not any(marker in text for marker in expected)]
        assert not unexpected, unexpected
        assert len(errors) == 2, errors
        browser.close()
    print("browser checks passed")


if __name__ == "__main__":
    main()
