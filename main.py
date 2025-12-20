from fastapi import FastAPI, UploadFile, File, Form, Request, Header, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import os, json, uuid, glob
from datetime import datetime

app = FastAPI()

# ============================
# 基本設定 / 資料夾（僅暫存）
# ============================
os.makedirs("data", exist_ok=True)
os.makedirs("uploads", exist_ok=True)

# 僅為了「當下顯示結果頁圖片」而掛載；回寫即刪
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

# Templates
templates = Jinja2Templates(directory="templates")

# 內網 API Key（由環境變數提供）
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "").strip()


def require_internal_key(x_api_key: str | None):
    if INTERNAL_API_KEY:
        if not x_api_key or x_api_key.strip() != INTERNAL_API_KEY:
            raise HTTPException(status_code=401, detail="Unauthorized")


# ============================
# 工具函式
# ============================

def guess_ext(upload: UploadFile) -> str:
    filename = (upload.filename or "").lower()
    if filename.endswith(".png"):
        return ".png"
    if filename.endswith(".jpeg"):
        return ".jpeg"
    if filename.endswith(".jpg"):
        return ".jpg"
    ctype = (upload.content_type or "").lower()
    if ctype == "image/png":
        return ".png"
    if ctype == "image/jpeg":
        return ".jpg"
    return ".jpg"


def save_case(case_id: str, data: dict):
    path = f"data/{case_id}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def load_case(case_id: str) -> dict:
    path = f"data/{case_id}.json"
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Case not found")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def delete_case_files(case_id: str):
    # 刪除該 case 的 JSON
    json_path = f"data/{case_id}.json"
    try:
        if os.path.exists(json_path):
            os.remove(json_path)
    except Exception as e:
        print(f"[WARN] delete json failed: {json_path} ({e})")

    # 刪除該 case 的圖片（jpg/png/jpeg）
    for p in glob.glob(f"uploads/{case_id}.*"):
        try:
            os.remove(p)
        except Exception as e:
            print(f"[WARN] delete image failed: {p} ({e})")


# ============================
# Routes
# ============================

# 1) 表單頁
@app.get("/form", response_class=HTMLResponse)
async def form_page(request: Request):
    return templates.TemplateResponse("form.html", {"request": request})


# 2) 接收資料（外網暫存 → 等內網回寫）
@app.post("/submit_case")
async def submit_case(
    name: str = Form(...),
    phone: str = Form(...),
    line_id: str = Form(...),
    image: UploadFile = File(...)
):
    case_id = str(uuid.uuid4())
    created_at = datetime.now().isoformat(timespec="seconds")

    # 暫存圖片
    ext = guess_ext(image)
    img_path = f"uploads/{case_id}{ext}"
    with open(img_path, "wb") as f:
        f.write(await image.read())

    # 暫存案件資料（AI 尚未回寫）
    record = {
        "id": case_id,
        "created_at": created_at,
        "name": name,
        "phone": phone,
        "line_id": line_id,
        "image_url": f"/uploads/{case_id}{ext}",
        "ai_level": None,
        "ai_prob": None,
        "ai_suggestion": None,
        "status": "pending",
    }

    save_case(case_id, record)

    # 直接導向結果頁（顯示分析中）
    return RedirectResponse(url=f"/result/{case_id}", status_code=302)


# 3) 結果頁（僅當下有效；回寫即刪後重整會 404）
@app.get("/result/{case_id}", response_class=HTMLResponse)
def result_page(request: Request, case_id: str):
    case = load_case(case_id)
    return templates.TemplateResponse("result.html", {"request": request, "case": case})


# 4) 內網 pull：取得 pending 案件（需要 X-API-KEY）
@app.get("/pending")
async def pending(x_api_key: str | None = Header(default=None, alias="X-API-KEY")):
    require_internal_key(x_api_key)

    results = []
    for file in os.listdir("data"):
        if file.endswith(".json"):
            with open(f"data/{file}", "r", encoding="utf-8") as f:
                rec = json.load(f)
                if rec.get("status") == "pending":
                    results.append(rec)
    return results


# 5) 內網標記案件已取走（需要 X-API-KEY）
@app.post("/mark_as_taken")
async def mark_as_taken(
    id: str = Form(...),
    x_api_key: str | None = Header(default=None, alias="X-API-KEY")
):
    require_internal_key(x_api_key)

    case = load_case(id)
    case["status"] = "taken"
    save_case(id, case)
    return {"status": "ok"}


# 6) 內網回寫 AI 結果（需要 X-API-KEY）
#    收到回寫後：立刻刪除該 case 的 json + 圖片（一次性顯示）
@app.post("/update_ai_result")
async def update_ai_result(
    id: str = Form(...),
    ai_level: int = Form(...),
    ai_prob: float = Form(...),
    ai_suggestion: str = Form(...),
    status: str = Form("done"),
    x_api_key: str | None = Header(default=None, alias="X-API-KEY")
):
    require_internal_key(x_api_key)

    case = load_case(id)
    case["ai_level"] = ai_level
    case["ai_prob"] = ai_prob
    case["ai_suggestion"] = ai_suggestion
    case["status"] = status
    case["updated_at"] = datetime.now().isoformat(timespec="seconds")
    save_case(id, case)

    # 立刻刪除該使用者的所有暫存資料
    delete_case_files(id)

    return {"status": "ok"}


@app.get("/")
def home():
    return {"message": "外網運作正常（回寫即刪）"}
