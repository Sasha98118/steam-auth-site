from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import httpx
from urllib.parse import urlencode
import os
from dotenv import load_dotenv
import secrets
from jinja2 import Environment, FileSystemLoader
import asyncio
from typing import Optional
import socket

try:
    import a2s
except ImportError:
    print("ВНИМАНИЕ: python-a2s не установлен. Выполните: pip install python-a2s")
    a2s = None

load_dotenv()

app = FastAPI(title="Steam Auth App")

app.mount("/static", StaticFiles(directory="static"), name="static")

jinja_env = Environment(loader=FileSystemLoader("templates"))

STEAM_API_KEY = os.getenv("STEAM_API_KEY")
SECRET_KEY = os.getenv("SECRET_KEY")

CS2_SERVER_IP = os.getenv("CS2_SERVER_IP")
CS2_SERVER_PORT = int(os.getenv("CS2_SERVER_PORT", "27015"))
CS2_SERVER_NAME = os.getenv("CS2_SERVER_NAME", "PCS Cyber CS2 Server")
CS2_SERVER_PASSWORD = os.getenv("CS2_SERVER_PASSWORD", "")

PTERO_API_URL = os.getenv("PTERO_API_URL")
PTERO_API_KEY = os.getenv("PTERO_API_KEY")
PTERO_SERVER_ID = os.getenv("PTERO_SERVER_ID")

user_sessions = {}

def generate_session_token() -> str:
    return secrets.token_urlsafe(32)

def render_template(template_name: str, **kwargs):
    template = jinja_env.get_template(template_name)
    return HTMLResponse(content=template.render(**kwargs))

def get_steam_user_data(steam_id: str):
    if not STEAM_API_KEY:
        print("Ошибка: Не настроен STEAM_API_KEY в файле .env")
        return None
    
    url = "http://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/"
    params = {
        "key": STEAM_API_KEY,
        "steamids": steam_id
    }
    
    try:
        with httpx.Client() as client:
            response = client.get(url, params=params, timeout=10.0)
            response.raise_for_status()
            data = response.json()
            
            players = data.get("response", {}).get("players", [])
            if players:
                player = players[0]
                return {
                    "steam_id": player.get("steamid"),
                    "nickname": player.get("personaname", "Игрок Steam"),
                    "avatar": player.get("avatar", ""),
                    "avatar_medium": player.get("avatarmedium", ""),
                    "avatar_full": player.get("avatarfull", ""),
                    "profile_url": player.get("profileurl", "")
                }
            else:
                print(f"Пользователь с ID {steam_id} не найден")
                return None
    except Exception as e:
        print(f"Ошибка при запросе к Steam API: {e}")
        return None

async def get_server_status_from_ptero():
    if not all([PTERO_API_URL, PTERO_API_KEY, PTERO_SERVER_ID]):
        print("Pterodactyl API не настроен, получение статуса через API пропущено.")
        return None
        
    headers = {
        "Authorization": f"Bearer {PTERO_API_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    url = f"{PTERO_API_URL}/servers/{PTERO_SERVER_ID}/resources"
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=5.0)
            response.raise_for_status()
            data = response.json()
            attributes = data.get("attributes", {})
            current_state = attributes.get("current_state", "offline")
            
            return {
                "status": current_state,
                "players": attributes.get("resources", {}).get("players", 0)
            }
    except Exception as e:
        print(f"Ошибка запроса к Pterodactyl API: {e}")
        return None

def get_server_status_direct(ip, port):
    if a2s is None:
        print("Библиотека a2s не установлена, прямой запрос невозможен")
        return {"status": "unknown", "player_count": 0, "map_name": "—", "players_list": []}
    
    if not ip:
        print("IP сервера не настроен")
        return {"status": "offline", "player_count": 0, "map_name": "—", "players_list": []}
    
    try:
        address = (ip, port)
        info = a2s.info(address, timeout=2.0)
        players = a2s.players(address, timeout=2.0)
        
        return {
            "status": "online",
            "server_name": info.server_name,
            "map_name": info.map_name,
            "player_count": info.player_count,
            "max_players": info.max_players,
            "players_list": [p.name for p in players]
        }
    except (socket.timeout, socket.gaierror, ConnectionRefusedError, OSError) as e:
        print(f"Не удалось подключиться к серверу {ip}:{port}: {e}")
        return {"status": "offline", "player_count": 0, "map_name": "—", "players_list": []}
    except Exception as e:
        print(f"Ошибка при запросе к серверу: {e}")
        return {"status": "offline", "player_count": 0, "map_name": "—", "players_list": []}

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    session_token = request.cookies.get("session_token")
    user_data = None
    
    if session_token and session_token in user_sessions:
        user_data = user_sessions[session_token]
    
    return render_template("index.html", user=user_data)

@app.get("/login/steam")
async def steam_login():
    DOMAIN = os.getenv("DOMAIN", "steam-auth-site.onrender.com")
    PROTOCOL = "https" if os.getenv("USE_HTTPS", "False") == "True" else "http"
    
    return_url = f"{PROTOCOL}://{DOMAIN}/auth/steam/callback"
    
    params = {
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.mode": "checkid_setup",
        "openid.return_to": return_url,
        "openid.realm": f"{PROTOCOL}://{DOMAIN}",
        "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select"
    }
    
    steam_openid_url = "https://steamcommunity.com/openid/login"
    redirect_url = f"{steam_openid_url}?{urlencode(params)}"
    
    return RedirectResponse(url=redirect_url)

@app.get("/auth/steam/callback")
async def steam_callback(request: Request):
    params = dict(request.query_params)
    
    if not params or "openid.ns" not in params:
        raise HTTPException(status_code=400, detail="Неверный запрос авторизации от Steam")
    
    claimed_id = params.get("openid.claimed_id", "")
    if not claimed_id:
        raise HTTPException(status_code=400, detail="Не получен идентификатор пользователя Steam")
    
    steam_id = claimed_id.split("/")[-1]

    if not steam_id or not steam_id.isdigit():
        raise HTTPException(status_code=400, detail="Неверный формат Steam ID")

    validation_params = {}
    for key, value in params.items():
        if key.startswith("openid."):
            validation_params[key] = value

    validation_params["openid.mode"] = "check_authentication"
    verification_url = "https://steamcommunity.com/openid/login"

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                verification_url,
                data=validation_params,
                timeout=10.0,
                follow_redirects=False
            )
            
            print(f"Статус верификации Steam: {response.status_code}")
            print(f"Ответ Steam: {response.text}")

            if response.status_code != 200 or "is_valid:true" not in response.text:
                raise HTTPException(
                    status_code=401, 
                    detail=f"Ошибка верификации Steam. Ответ: {response.text[:200]}"
                )
    except Exception as e:
        print(f"Исключение при верификации: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка при подключении к Steam: {e}")

    user_data = get_steam_user_data(steam_id)
    
    if not user_data:
        raise HTTPException(status_code=404, detail="Не удалось получить данные пользователя из Steam")
    
    session_token = generate_session_token()
    user_sessions[session_token] = user_data
    
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key="session_token",
        value=session_token,
        max_age=60*60*24*30,
        httponly=True,
        secure=True,
        samesite="lax"
    )
    
    return response

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("session_token")
    return response

@app.get("/api/server/status")
async def api_server_status(request: Request):
    session_token = request.cookies.get("session_token")
    if not session_token or session_token not in user_sessions:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    
    ptero_status = await get_server_status_from_ptero()
    direct_status = get_server_status_direct(CS2_SERVER_IP, CS2_SERVER_PORT)
    
    final_status = {
        "server_name": CS2_SERVER_NAME,
        "map_name": direct_status.get("map_name", "—"),
        "player_count": direct_status.get("player_count", 0),
        "max_players": direct_status.get("max_players", 10),
        "status": direct_status.get("status", "offline"),
        "players_list": direct_status.get("players_list", [])
    }
    
    if final_status["status"] == "offline" and ptero_status and ptero_status["status"] == "running":
        final_status["status"] = "online"
    
    return final_status

@app.get("/api/server/connect")
async def api_server_connect(request: Request):
    session_token = request.cookies.get("session_token")
    if not session_token or session_token not in user_sessions:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    
    if not CS2_SERVER_IP:
        raise HTTPException(status_code=500, detail="IP сервера не настроен")
    
    # Возвращаемся к steam://connect/ — он открывает Steam
    connection_string = f"steam://connect/{CS2_SERVER_IP}:{CS2_SERVER_PORT}"
    if CS2_SERVER_PASSWORD:
        connection_string += f"/{CS2_SERVER_PASSWORD}"
    
    print(f"Сгенерирована ссылка для пользователя {user_sessions[session_token]['nickname']}")
    
    return {
        "connect_url": connection_string
    }

if __name__ == "__main__":
    import uvicorn
    print("=" * 50)
    print("🚀 Сайт запускается...")
    print("📍 Открой в браузере: http://localhost:8000")
    print("=" * 50)
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)