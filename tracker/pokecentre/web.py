"""Tiny single-page web UI for toggling alerts / autobuy per product.

Run:  uvicorn pokecentre.web:app --host 0.0.0.0 --port 8000
Auth: HTTP basic via env vars WEB_USER and WEB_PASS. Refuses to start without them.
Reads the same SQLite DB the tracker writes to (set TRACKER_DB env var).
"""
from __future__ import annotations

import html
import os
import secrets

from fastapi import Depends, FastAPI, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .store import Store


_DB_PATH = os.environ.get("TRACKER_DB", "tracker.db")
_USER = os.environ.get("WEB_USER", "")
_PASS = os.environ.get("WEB_PASS", "")

if not _USER or not _PASS:
    raise RuntimeError(
        "WEB_USER and WEB_PASS env vars are required. Refusing to start an unauthenticated UI."
    )

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_basic = HTTPBasic()


def _auth(creds: HTTPBasicCredentials = Depends(_basic)) -> str:
    ok_user = secrets.compare_digest(creds.username, _USER)
    ok_pass = secrets.compare_digest(creds.password, _PASS)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    return creds.username


def _store() -> Store:
    return Store(_DB_PATH)


@app.get("/healthz")
def healthz() -> dict:
    # Unauthenticated healthcheck for Render. Doesn't expose any data.
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index(_user: str = Depends(_auth), filter: str = "all") -> HTMLResponse:
    s = _store()
    products = s.all_products()
    prefs = s.all_preferences()

    if filter == "in_stock":
        products = [p for p in products if p.in_stock == 1]
    elif filter == "watched":
        products = [
            p for p in products
            if (prefs.get(p.url) and (prefs[p.url].alert_enabled or prefs[p.url].autobuy_enabled))
        ]

    rows: list[str] = []
    for p in products:
        pref = prefs.get(p.url)
        alert_on = pref.alert_enabled if pref else True
        autobuy_on = pref.autobuy_enabled if pref else False
        stock_badge = (
            '<span class="badge in">in stock</span>' if p.in_stock == 1
            else '<span class="badge oos">out</span>' if p.in_stock == 0
            else '<span class="badge unk">unknown</span>'
        )
        img = (
            f'<img src="{html.escape(p.image_url)}" alt="" loading="lazy">'
            if p.image_url else '<div class="noimg">no image</div>'
        )
        desc = html.escape((p.description or "")[:200])
        rows.append(f"""
        <div class="card">
          <div class="thumb">{img}</div>
          <div class="meta">
            <div class="title"><a href="{html.escape(p.url)}" target="_blank" rel="noopener">{html.escape(p.title or p.url)}</a></div>
            <div class="sub">{html.escape(p.price or "—")} · {stock_badge}</div>
            <div class="desc">{desc}</div>
          </div>
          <form method="post" action="/toggle" class="toggles">
            <input type="hidden" name="url" value="{html.escape(p.url)}">
            <input type="hidden" name="filter" value="{html.escape(filter)}">
            <label class="toggle">
              <input type="checkbox" name="alert" {"checked" if alert_on else ""}>
              <span>alert</span>
            </label>
            <label class="toggle">
              <input type="checkbox" name="autobuy" {"checked" if autobuy_on else ""}>
              <span>autobuy</span>
            </label>
            <button type="submit">save</button>
          </form>
        </div>
        """)

    body = "\n".join(rows) if rows else "<p class='empty'>No products tracked yet. Wait for the tracker to populate.</p>"

    return HTMLResponse(_PAGE.format(
        body=body,
        count=len(products),
        filter_all="active" if filter == "all" else "",
        filter_stock="active" if filter == "in_stock" else "",
        filter_watched="active" if filter == "watched" else "",
    ))


@app.post("/toggle")
def toggle(
    url: str = Form(...),
    alert: str | None = Form(None),
    autobuy: str | None = Form(None),
    filter: str = Form("all"),
    _user: str = Depends(_auth),
) -> RedirectResponse:
    s = _store()
    s.set_preference(
        url=url,
        alert_enabled=(alert is not None),
        autobuy_enabled=(autobuy is not None),
    )
    return RedirectResponse(url=f"/?filter={filter}", status_code=303)


_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pokemon Centre Tracker</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font: 15px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         margin: 0; background: #f6f7f9; color: #111; }}
  header {{ position: sticky; top: 0; background: #fff; border-bottom: 1px solid #e2e4e8;
           padding: 14px 20px; display: flex; gap: 16px; align-items: center; z-index: 10; }}
  header h1 {{ font-size: 17px; margin: 0; }}
  header .count {{ color: #6a7280; font-size: 13px; }}
  nav a {{ display: inline-block; padding: 6px 12px; border-radius: 6px; color: #374151;
          text-decoration: none; font-size: 13px; }}
  nav a.active {{ background: #111; color: #fff; }}
  main {{ max-width: 980px; margin: 0 auto; padding: 16px; }}
  .card {{ background: #fff; border: 1px solid #e2e4e8; border-radius: 10px;
          padding: 14px; margin-bottom: 10px; display: grid;
          grid-template-columns: 80px 1fr auto; gap: 14px; align-items: center; }}
  .thumb img {{ width: 80px; height: 80px; object-fit: contain; border-radius: 6px; background: #f0f2f5; }}
  .thumb .noimg {{ width: 80px; height: 80px; display: grid; place-items: center;
                  background: #f0f2f5; color: #9ca3af; font-size: 11px; border-radius: 6px; }}
  .title a {{ color: #111; text-decoration: none; font-weight: 600; }}
  .title a:hover {{ text-decoration: underline; }}
  .sub {{ color: #6a7280; font-size: 13px; margin-top: 2px; }}
  .desc {{ color: #6a7280; font-size: 12px; margin-top: 4px; max-height: 2.8em; overflow: hidden; }}
  .badge {{ display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11px;
           font-weight: 600; margin-left: 6px; }}
  .badge.in {{ background: #dcfce7; color: #166534; }}
  .badge.oos {{ background: #fee2e2; color: #991b1b; }}
  .badge.unk {{ background: #e5e7eb; color: #4b5563; }}
  .toggles {{ display: flex; gap: 10px; align-items: center; }}
  .toggle {{ display: inline-flex; align-items: center; gap: 4px; font-size: 13px;
            background: #f3f4f6; padding: 6px 10px; border-radius: 6px; cursor: pointer; }}
  .toggle input {{ accent-color: #111; }}
  .toggles button {{ background: #111; color: #fff; border: 0; padding: 7px 12px;
                    border-radius: 6px; font-size: 13px; cursor: pointer; }}
  .empty {{ text-align: center; padding: 40px; color: #6a7280; }}
  @media (max-width: 640px) {{
    .card {{ grid-template-columns: 60px 1fr; }}
    .thumb img, .thumb .noimg {{ width: 60px; height: 60px; }}
    .toggles {{ grid-column: 1 / -1; justify-content: flex-end; }}
  }}
</style>
</head><body>
<header>
  <h1>Pokemon Centre Tracker</h1>
  <span class="count">{count} items</span>
  <nav>
    <a href="/?filter=all" class="{filter_all}">All</a>
    <a href="/?filter=in_stock" class="{filter_stock}">In stock</a>
    <a href="/?filter=watched" class="{filter_watched}">Watched</a>
  </nav>
</header>
<main>
  {body}
</main>
</body></html>
"""
