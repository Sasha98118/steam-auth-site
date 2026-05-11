from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import httpx
from urllib.parse import urlencode
import os
from dotenv import load_dotenv
import secrets
from jinja2 import Environment, FileSystemLoader

load_dotenv()

app = FastAPI(title="Steam Auth App")

# Подключаем статику
app.mount("/static", StaticFiles(directory="static"), name="static")

# Настраиваем Jinja2 напрямую (в обход FastAPI-обёртки)
jinja_env = Environment(loader=FileSystemLoader("templates"))

# Получаем API-ключ из переменных окружения
STEAM_API_KEY = os.getenv("STEAM_API_KEY")
SECRET_KEY = os.getenv("SECRET_KEY")

# Временное хранилище для данных пользователя
user_sessions = {}

def generate_session_token() -> str:
    """Создаёт случайную строку для идентификации сессии пользователя"""
    return secrets.token_urlsafe(32)

def render_template(template_name: str, **kwargs):
    """Упрощённая функция рендеринга шаблонов"""
    template = jinja_env.get_template(template_name)
    return HTMLResponse(content=template.render(**kwargs))

def get_steam_user_data(steam_id: str):
    """Запрашивает у Steam API информацию о пользователе"""
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

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Главная страница"""
    session_token = request.cookies.get("session_token")
    user_data = None
    
    if session_token and session_token in user_sessions:
        user_data = user_sessions[session_token]
    
    # Используем нашу функцию render_template вместо templates.TemplateResponse
    return render_template("index.html", user=user_data)

@app.get("/login/steam")
async def steam_login():
    """Шаг 1 авторизации через OpenID 2.0"""
    DOMAIN = os.getenv("DOMAIN", "localhost:8000")
    PROTOCOL = "https" if os.getenv("USE_HTTPS", "False") == "True" else "http"
    
    return_url = f"{PROTOCOL}://{DOMAIN}/auth/steam/callback"
    
    params = {
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.mode": "checkid_setup",
        "openid.return_to": return_url,
        "openid.realm": "http://localhost:8000",
        "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select"
    }
    
    steam_openid_url = "https://steamcommunity.com/openid/login"
    redirect_url = f"{steam_openid_url}?{urlencode(params)}"
    
    return RedirectResponse(url=redirect_url)

@app.get("/auth/steam/callback")
async def steam_callback(request: Request):
    """Шаг 2 авторизации. Steam возвращает пользователя сюда"""
    params = dict(request.query_params)
    
    if not params or "openid.ns" not in params:
        raise HTTPException(status_code=400, detail="Неверный запрос авторизации от Steam")
    
    claimed_id = params.get("openid.claimed_id", "")
    if not claimed_id:
        raise HTTPException(status_code=400, detail="Не получен идентификатор пользователя Steam")
    
    steam_id = claimed_id.split("/")[-1]

    if not steam_id or not steam_id.isdigit():
        raise HTTPException(status_code=400, detail="Неверный формат Steam ID")

    # --- НАЧАЛО ИСПРАВЛЕННОГО БЛОКА ВЕРИФИКАЦИИ ---
    # Готовим параметры для запроса верификации в Steam.
    # Steam требует отправить обратно ВСЕ параметры, начинающиеся с "openid.",
    # но с измененным режимом (mode) на "check_authentication".
    validation_params = {}
    for key, value in params.items():
        if key.startswith("openid."):
            validation_params[key] = value

    # Критически важно: меняем режим с 'id_res' на 'check_authentication'
    validation_params["openid.mode"] = "check_authentication"

    # URL для верификации
    verification_url = "https://steamcommunity.com/openid/login"

    try:
        # Отправляем POST-запрос на проверку
        async with httpx.AsyncClient() as client:
            # Явно указываем data= для формы urlencoded
            response = await client.post(
                verification_url,
                data=validation_params,  # <-- отправляем как данные формы
                timeout=10.0,
                follow_redirects=False   # Не нужно следовать редиректам
            )
            
            # Распечатаем в терминале, что ответил Steam (для отладки)
            print(f"Статус верификации Steam: {response.status_code}")
            print(f"Ответ Steam: {response.text}")

            # Проверяем, что в ответе есть строка "is_valid:true"
            if response.status_code != 200 or "is_valid:true" not in response.text:
                # Если что-то не так, выводим ошибку с деталями ответа
                raise HTTPException(
                    status_code=401, 
                    detail=f"Ошибка верификации Steam. Ответ: {response.text[:200]}"
                )
    except Exception as e:
        print(f"Исключение при верификации: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка при подключении к Steam: {e}")
    # --- КОНЕЦ ИСПРАВЛЕННОГО БЛОКА ---

    # Всё хорошо! Теперь получаем детальную информацию о пользователе через Steam API
    user_data = get_steam_user_data(steam_id)
    
    if not user_data:
        raise HTTPException(status_code=404, detail="Не удалось получить данные пользователя из Steam")
    
    # Создаём сессию
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
    """Выход из аккаунта"""
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("session_token")
    return response

if __name__ == "__main__":
    import uvicorn
    print("=" * 50)
    print("🚀 Сайт запускается...")
    print("📍 Открой в браузере: http://localhost:8000")
    print("=" * 50)
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)