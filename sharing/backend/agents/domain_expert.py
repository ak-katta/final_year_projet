"""Agent #7 — Domain Expert.

One class, N templates. Loads site-specific test guidance into state.plan_notes.
"""

from state import QAState, SiteType
from agents.base import Agent


TEMPLATES: dict[SiteType, str] = {

    SiteType.ECOMMERCE: """
Test categories you MUST cover:
1. Product discovery — search, filter, sort, category nav
2. Product page      — images, price, description, stock, variants
3. Cart              — add, remove, update qty, subtotal calculation
4. Checkout          — guest vs login, address form, shipping, coupon
5. Payment           — invalid card, expired card, empty fields
6. Order confirmation— order number shown, email sent
7. Edge cases        — out-of-stock, max qty, currency, tax
""",

    SiteType.BLOG: """
Test categories you MUST cover:
1. Homepage       — post list, featured, pagination
2. Article page   — title, author, date, content renders
3. Comments       — submit empty, submit valid, XSS payload escaped
4. Search         — empty query, no results, special chars
5. Categories/tags— filter works, empty state
6. Share buttons  — correct URLs
7. RSS/sitemap    — reachable, well-formed
""",

    SiteType.SAAS: """
Test categories you MUST cover:
1. Auth          — login, logout, wrong password, session persistence
2. CRUD          — create, read, update, delete for primary resource
3. Tables        — sort, filter, paginate, search
4. Forms         — validation, required fields, edge cases
5. Settings      — save, cancel, reset
6. Billing       — plan display, upgrade flow, invoice list
7. Permissions   — role-based UI elements
""",

    SiteType.FORM: """
Test categories you MUST cover:
1. Empty submit      — required field errors shown
2. Invalid email     — proper validation message
3. Max length        — field caps input
4. Special chars     — quotes, unicode, emoji
5. XSS payload       — <script>alert(1)</script> is escaped
6. SQL injection     — ' OR 1=1 -- is escaped
7. File upload       — wrong type, too large (if present)
8. Success path      — confirmation shown, fields cleared
""",

    SiteType.SEARCH: """
Test categories you MUST cover:
1. Empty query
2. Common query         — results appear
3. No results           — empty state shown
4. Special chars        — regex chars, quotes
5. Long query           — 500+ chars
6. Pagination / infinite scroll
7. Filter combinations
""",

    SiteType.DOCS: """
Test categories you MUST cover:
1. Sidebar nav    — every section clickable
2. Search         — finds code samples
3. Code blocks    — copy button, syntax highlight
4. Anchor links   — deep links work
5. Prev/next nav
6. Version switcher (if present)
""",

    SiteType.PORTFOLIO: """
Test categories you MUST cover:
1. Nav links resolve
2. Project thumbnails load
3. Contact form works
4. Social links correct
5. Responsive at 375/768/1440
""",

    SiteType.SOCIAL: """
Test categories you MUST cover:
1. Feed loads, infinite scroll
2. Post interactions — like, comment, share
3. Profile pages
4. Follow / unfollow
5. Search users/posts
""",

    SiteType.OTHER: """
General test categories:
1. All nav links resolve (no 404)
2. All forms submit without error
3. All buttons clickable (no console errors)
4. Images load
5. No console errors on load
""",
}


class DomainExpertAgent(Agent):
    name = "domain_expert"
    purpose = "plan"

    def run(self, state: QAState) -> QAState:
        template = TEMPLATES.get(state.site_type, TEMPLATES[SiteType.OTHER])
        state.plan_notes = template.strip()
        self.log(state, f"loaded template for {state.site_type.value}")
        return state