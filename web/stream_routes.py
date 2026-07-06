import math
import secrets
import mimetypes
from info import BIN_CHANNEL, MAX_BTN, PREMIUM_PLANS, PAYMENT_QR_CODE, PAYMENT_ID, PAYMENT_TYPE, OWNER_USERNAME, TMDB_API_KEY
from utils import temp, get_size, handle_next_back, get_plan_name
from aiohttp import web
from web.utils.custom_dl import TGCustomYield, chunk_size, offset_fix
from web.utils.render_template import media_watch, error_tmplt, webapp_template, payment_template, no_tmdb_template
from database.ia_filterdb import get_search_results
from database.users_chats_db import db
import json, io, aiohttp, html
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

routes = web.RouteTableDef()

TMDB_BASE = "https://api.themoviedb.org/3"


def masked_link_page(title, message, button_html=""):
    safe_title = html.escape(title)
    safe_message = html.escape(message)
    return f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title}</title>
  <style>
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; font-family: Arial, sans-serif; background: #0f172a; color: #e5e7eb; }}
    .card {{ width: min(92vw, 460px); padding: 32px; border-radius: 22px; background: #111827; text-align: center; box-shadow: 0 20px 60px rgba(0,0,0,.35); }}
    h1 {{ margin: 0 0 14px; font-size: 28px; }}
    p {{ margin: 0 0 24px; color: #cbd5e1; line-height: 1.5; }}
    a.button {{ display: inline-block; padding: 14px 22px; border-radius: 999px; background: #22c55e; color: #06130a; text-decoration: none; font-weight: 700; }}
  </style>
</head>
<body>
  <main class="card">
    <h1>{safe_title}</h1>
    <p>{safe_message}</p>
    {button_html}
  </main>
</body>
</html>
"""


@routes.get("/r/{hash_id}", allow_head=True)
async def masked_shortlink_handler(request):
    hash_id = request.match_info['hash_id']
    link = await db.get_masked_link(hash_id)
    if not link:
        return web.Response(
            text=masked_link_page("Link Not Found", "This masked link is invalid or no longer exists."),
            content_type='text/html',
            status=404
        )

    if link.get('used'):
        return web.Response(
            text=masked_link_page("Link Expired", "This one-time link was already used. Please request a new one."),
            content_type='text/html',
            status=410
        )

    real_url = link['real_url']
    user_agent = request.headers.get('User-Agent', '').lower()
    if 'telegram' in user_agent:
        scheme = 'http' if real_url.startswith('http://') else 'https'
        intent_url = html.escape(real_url.replace('https://', '', 1).replace('http://', '', 1), quote=True)
        button = f'<a class="button" href="intent://{intent_url}#Intent;scheme={scheme};package=com.android.chrome;end">Open in Chrome</a>'
        return web.Response(
            text=masked_link_page("Open in Chrome", "For security, open this link in an external Chrome browser to continue.", button),
            content_type='text/html'
        )

    if not await db.mark_masked_link_used(hash_id):
        return web.Response(
            text=masked_link_page("Link Expired", "This one-time link was already used. Please request a new one."),
            content_type='text/html',
            status=410
        )
    raise web.HTTPFound(real_url)

@routes.get("/watch/{message_id}")
async def watch_handler(request):
    try:
        message_id = int(request.match_info['message_id'])
        return web.Response(text=await media_watch(message_id), content_type='text/html')
    except Exception as e:
        return web.Response(text=error_tmplt, content_type='text/html')

@routes.get("/download/{message_id}")
async def download_handler(request):
    try:
        message_id = int(request.match_info['message_id'])
        return await media_download(request, message_id)
    except:
        return web.Response(text=error_tmplt, content_type='text/html')
        

@routes.get("/", allow_head=True)
async def webapp_route_handler(request):
    if not TMDB_API_KEY:
        return web.Response(text=no_tmdb_template, content_type='text/html')
    return web.Response(text=webapp_template, content_type='text/html')


@routes.get("/activate-plan", allow_head=True)
async def activate_plan_handler(request):
    FRONTEND_PLANS = {}
    for days, details in PREMIUM_PLANS.items():
        nice_name = get_plan_name(days)
        FRONTEND_PLANS[str(days)] = [nice_name, details[0], details[1]]

    html_content = payment_template.replace('{{QR_IMG}}', PAYMENT_QR_CODE)
    html_content = html_content.replace('{{PAYM_ID}}', PAYMENT_ID)
    html_content = html_content.replace('{{PAYM_TYPE}}', PAYMENT_TYPE)
    html_content = html_content.replace('{{PLANS_JSON}}', json.dumps(FRONTEND_PLANS))
    
    return web.Response(text=html_content, content_type='text/html')

@routes.post("/submit-payment")
async def submit_payment_handler(request):
    try:
        data = await request.post()
        days_str = data.get('days') 
        user_id = data.get('user_id')
        user_name = data.get('user_name')
        slip_field = data.get('slip')
        plan_days = int(days_str) if days_str and days_str.isdigit() else 0
        
        if plan_days not in PREMIUM_PLANS:
            return web.json_response({"status": "error", "message": "Invalid plan selected."}, status=400)
            
        if not slip_field:
            return web.json_response({"status": "error", "message": "No slip uploaded."}, status=400)

        file_bytes = slip_field.file.read()
        if len(file_bytes) > 5242880:
            return web.json_response({"status": "error", "message": "Image too large. Max 5MB."}, status=413)
        
        photo_io = io.BytesIO(file_bytes)
        photo_io.name = f"{user_id}_payment_slip.jpg" 
        bot_plan_name = get_plan_name(plan_days)
        btn = [[
            InlineKeyboardButton('✅ Accept', callback_data=f'accept_payment-{user_id}-{plan_days}'),
            InlineKeyboardButton('❌ Reject', callback_data=f'reject_payment-{user_id}-{plan_days}'),
        ]]
        text = f"""💰 New Payment Received!\n\nUser: <a href="tg://user?id={user_id}">{user_name}</a>\nUser ID: <code>{user_id}</code>\nPlan: {bot_plan_name} ({plan_days} Days)"""
        await temp.BOT.send_photo(chat_id=OWNER_USERNAME, photo=photo_io, caption=text, reply_markup=InlineKeyboardMarkup(btn))
        await temp.BOT.send_message(chat_id=int(user_id), text=f"Thank you! Your payment slip has been sent to the owner. Once it is verified, your Premium Plan [{bot_plan_name}] will be activated soon.\n\nSupport: @{OWNER_USERNAME}")
        
        return web.json_response({"status": "success"})
        
    except Exception as e:
        print(f"Server Error: {e}")
        return web.json_response({"status": "error", "message": "Server error processing payload."}, status=500)


@routes.get("/api/search")
async def api_search_handler(request):
    query = request.query.get('q', '').strip()
    offset = int(request.query.get('offset', 0))
  
    files = await get_search_results(query)
    files, next_offset, total_results = await handle_next_back(files, offset=offset, max_results=MAX_BTN)
    
    formatted_files = []
    if files:
        for file in files:
            formatted_files.append({
                "id": str(file['_id']),
                "name": file.get('file_name', 'Unknown'),
                "size": get_size(file.get('file_size', 0))
            })
 
    return web.json_response({
        "files": formatted_files,
        "next_offset": next_offset if next_offset != 0 else None,
        "total_results": total_results,
        "current_offset": offset,
        "max_btn": MAX_BTN,
        "bot_username": temp.U_NAME
    })


@routes.get("/api/tmdb-search")
async def tmdb_search_handler(request):
    if not TMDB_API_KEY:
        return web.json_response({"results": [], "error": "TMDB API key not configured"}, status=503)
    query = request.query.get('q', '').strip()
    page = request.query.get('page', '1')
    if not query:
        return web.json_response({"results": []})
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{TMDB_BASE}/search/multi",
                params={"api_key": TMDB_API_KEY, "query": query, "page": page, "include_adult": "true"}
            ) as resp:
                data = await resp.json()
        results = []
        for r in data.get("results", []):
            if r.get("media_type") not in ["movie", "tv"]:
                continue
            title = r.get("title") or r.get("name", "")
            date = r.get("release_date") or r.get("first_air_date", "")
            year = date[:4] if date else ""
            poster = f"https://image.tmdb.org/t/p/w342{r['poster_path']}" if r.get("poster_path") else None
            backdrop = f"https://image.tmdb.org/t/p/w1280{r['backdrop_path']}" if r.get("backdrop_path") else None
            results.append({
                "id": r["id"],
                "title": title,
                "year": year,
                "type": r["media_type"],
                "rating": round(r.get("vote_average", 0), 1),
                "poster": poster,
                "backdrop": backdrop,
                "overview": r.get("overview", ""),
                "genres": r.get("genre_ids", [])
            })
        return web.json_response({"results": results, "total_pages": data.get("total_pages", 1)})
    except Exception as e:
        return web.json_response({"results": [], "error": str(e)}, status=500)


@routes.get("/api/tmdb-trending")
async def tmdb_trending_handler(request):
    if not TMDB_API_KEY:
        return web.json_response({"error": "TMDB API key not configured"}, status=503)
    try:
        async with aiohttp.ClientSession() as session:
            urls = [
                (f"{TMDB_BASE}/trending/all/week", {"api_key": TMDB_API_KEY}),
                (f"{TMDB_BASE}/movie/popular", {"api_key": TMDB_API_KEY, "page": "1"}),
                (f"{TMDB_BASE}/tv/popular", {"api_key": TMDB_API_KEY, "page": "1"}),
                (f"{TMDB_BASE}/movie/top_rated", {"api_key": TMDB_API_KEY, "page": "1"}),
            ]
            responses = []
            for url, params in urls:
                async with session.get(url, params=params) as r:
                    responses.append(await r.json())

        def fmt(items, media_type=None):
            out = []
            for r in items[:20]:
                mt = media_type or r.get("media_type", "movie")
                title = r.get("title") or r.get("name", "")
                date = r.get("release_date") or r.get("first_air_date", "")
                year = date[:4] if date else ""
                poster = f"https://image.tmdb.org/t/p/w342{r['poster_path']}" if r.get("poster_path") else None
                backdrop = f"https://image.tmdb.org/t/p/w1280{r['backdrop_path']}" if r.get("backdrop_path") else None
                out.append({
                    "id": r["id"], "title": title, "year": year, "type": mt,
                    "rating": round(r.get("vote_average", 0), 1),
                    "poster": poster, "backdrop": backdrop,
                    "overview": r.get("overview", ""),
                    "genres": r.get("genre_ids", [])
                })
            return out

        trending_all = fmt(responses[0].get("results", []))
        popular_movies = fmt(responses[1].get("results", []), "movie")
        popular_tv = fmt(responses[2].get("results", []), "tv")
        top_rated = fmt(responses[3].get("results", []), "movie")

        hero = next((x for x in trending_all if x["backdrop"]), trending_all[0] if trending_all else None)

        return web.json_response({
            "hero": hero,
            "trending": trending_all,
            "popular_movies": popular_movies,
            "popular_tv": popular_tv,
            "top_rated": top_rated,
            "bot_username": temp.U_NAME
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


@routes.get("/api/repair-status")
async def repair_status_handler(request):
    repair = await db.get_repair_mode()
    return web.json_response({"repair_mode": repair})


async def media_download(request, message_id: int):
    range_header = request.headers.get('Range')
    media_msg = await temp.BOT.get_messages(BIN_CHANNEL, message_id)
    media = getattr(media_msg, media_msg.media.value, None)
    file_size = media.file_size

    if range_header:
        byte_range = range_header.replace('bytes=', '', 1).split('-', 1)
        start, end = byte_range[0].strip(), byte_range[1].strip()
        if start:
            from_bytes = int(start)
            until_bytes = int(end) if end else file_size - 1
        else:
            suffix_length = int(end)
            from_bytes = max(file_size - suffix_length, 0)
            until_bytes = file_size - 1
    else:
        from_bytes = 0
        until_bytes = file_size - 1

    until_bytes = min(until_bytes, file_size - 1)
    if from_bytes < 0 or from_bytes >= file_size or from_bytes > until_bytes:
        return web.Response(
            status=416,
            headers={
                "Content-Range": f"bytes */{file_size}",
                "Accept-Ranges": "bytes",
            }
        )

    req_length = until_bytes - from_bytes + 1

    new_chunk_size = await chunk_size(req_length)
    offset = await offset_fix(from_bytes, new_chunk_size)
    first_part_cut = from_bytes - offset
    last_part_cut = (until_bytes % new_chunk_size) + 1
    part_count = math.ceil((first_part_cut + req_length) / new_chunk_size)
    body = TGCustomYield().yield_file(media_msg, offset, first_part_cut, last_part_cut, part_count,
                                      new_chunk_size)

    file_name = media.file_name if media.file_name \
        else f"{secrets.token_hex(2)}.jpeg"
    mime_type = media.mime_type if media.mime_type \
        else f"{mimetypes.guess_type(file_name)}"

    return_resp = web.Response(
        status=206 if range_header else 200,
        body=body,
        headers={
            "Content-Type": mime_type,
            "Content-Range": f"bytes {from_bytes}-{until_bytes}/{file_size}",
            "Content-Disposition": f'inline; filename="{file_name}"',
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
        }
    )

    return_resp.headers.add("Content-Length", str(req_length))

    return return_resp
