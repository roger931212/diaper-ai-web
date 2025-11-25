from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import os, json
from datetime import datetime

app = FastAPI()

# 資料夾
os.makedirs("data", exist_ok=True)
os.makedirs("uploads", exist_ok=True)

# 靜態檔案
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

# Templates
templates = Jinja2Templates(directory="templates")

# 1. 表單頁面
@app.get("/form", response_class=HTMLResponse)
async def form_page(request: Request):
    return templates.TemplateResponse("form.html", {"request": request})

# 2. 接收資料
@app.post("/submit_case")
async def submit_case(
    name: str = Form(...),
    phone: str = Form(...),
    nh_card: str = Form(...),
    id_number: str = Form(...),
    email: str = Form(...),
    image: UploadFile = File(...)
):
    case_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 儲存圖片
    img_path = f"uploads/{case_id}.jpg"
    with open(img_path, "wb") as f:
        f.write(await image.read())

    # 假AI（之後用內網跑）
    ai_level = 2
    ai_prob = 0.87
    ai_suggestion = "建議使用護臀膏並觀察 24–48 小時"

    # JSON
    record = {
        "id": case_id,
        "name": name,
        "phone": phone,
        "nh_card": nh_card,
        "id_number": id_number,
        "email": email,
        "image_url": f"/uploads/{case_id}.jpg",
        "ai_level": ai_level,
        "ai_prob": ai_prob,
        "ai_suggestion": ai_suggestion,
        "status": "pending"
    }

    with open(f"data/{case_id}.json", "w") as f:
        json.dump(record, f, ensure_ascii=False, indent=4)

    return {"message": "已成功送出，AI 已收到資料"}

# 3. pending 給內網抓
@app.get("/pending")
async def pending():
    results = []
    for file in os.listdir("data"):
        if file.endswith(".json"):
            with open(f"data/{file}", "r") as f:
                rec = json.load(f)
                if rec["status"] == "pending":
                    results.append(rec)
    return results

# 4. 標記已讀
@app.post("/mark_as_taken")
async def mark_as_taken(id: str):
    path = f"data/{id}.json"
    if not os.path.exists(path):
        return {"error": "not found"}

    with open(path, "r") as f:
        rec = json.load(f)
    rec["status"] = "taken"

    with open(path, "w") as f:
        json.dump(rec, f, ensure_ascii=False, indent=4)

    return {"status": "ok"}

@app.get("/")
def home():
    return {"message": "外網運作正常"}
